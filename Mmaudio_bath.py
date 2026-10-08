import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from queue import Queue
from tkinter import (Tk, Listbox, Text, END, messagebox, StringVar,
                     filedialog, ttk, BooleanVar, SINGLE)
from tkinter import VERTICAL, RIGHT, LEFT, BOTH, Y, X, W
from tkinter.ttk import Progressbar

import requests

# ================= 默认配置 =================
DEFAULT_CONFIG = {
    "comfy_url": "http://127.0.0.1:8188",
    "workflow_path": r"D:\Order\DeskTop\KF\Mmaudio_bath\mmaudio_NSFW_bath_with_api.json",
    "output_dir": r"E:\Ordrnary\ComfyUI-aki-v3\ComfyUI\output",
    "temp_dir": r"E:\Ordrnary\ComfyUI-aki-v3\ComfyUI\temp",
    "ffmpeg_path": "ffmpeg",
    "clear_output": True,
    "clear_temp": True,
    "clear_segments": True
}
SEGMENT_WINDOW = 7.0              # 基础窗口长度(秒)。12GB 显存下 10s 会 OOM，7s 留 ~2.3GB 余量安全；想更长可试 8s
OVERLAP = 2.0                     # 窗口重叠秒数；回到 2.0 给交叉淡化更长的混合区间，配合 cosine 等功率淡化消除接缝处的音量凹陷/卡顿
SCRIPT_DIR = Path.cwd()
CONFIG_FILE = SCRIPT_DIR / "config.json"
PROGRESS_FILE = SCRIPT_DIR / "progress.json"
LOG_FILE = SCRIPT_DIR / "process_log.txt"
SEGMENTS_BASE = SCRIPT_DIR / "segments"
FINISH_DIR = SCRIPT_DIR / "Finish"
DUCE_DIR = SCRIPT_DIR / "Duce"

# ================= 日志 =================
log_queue = Queue()

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    log_queue.put(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def log_exception(msg):
    log(f"❌ {msg}")
    log(traceback.format_exc())

# ================= 配置/进度管理 =================
def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in cfg:
                        cfg[k] = v
                return cfg
        except:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

def load_progress():
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("completed", []), data.get("in_progress", {})
        except:
            return [], {}
    return [], {}

def save_progress(completed_list, in_progress_dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "completed": completed_list,
            "in_progress": in_progress_dict
        }, f, indent=2)

def find_videos():
    exts = [".mp4", ".mov", ".avi", ".mkv"]
    files = []
    for ext in exts:
        files.extend(SCRIPT_DIR.glob(f"*{ext}"))
    return sorted(files, key=lambda x: x.name)

# ================= ComfyUI 交互 =================
def load_api_workflow(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "prompt" in data:
        prompt = data["prompt"]
    else:
        prompt = data
    return {k: v for k, v in prompt.items() if isinstance(v, dict)}

def submit_prompt(comfy_url, prompt):
    payload = {"prompt": prompt}
    try:
        r = requests.post(f"{comfy_url}/prompt", json=payload, timeout=30)
        if r.status_code != 200:
            log(f"提交失败: {r.text}")
            return None
        return r.json()["prompt_id"]
    except Exception as e:
        log(f"提交异常: {e}")
        return None

def wait_for_prompt(comfy_url, pid):
    while True:
        time.sleep(2)
        r = requests.get(f"{comfy_url}/history/{pid}")
        if r.status_code == 200 and pid in r.json():
            return r.json()[pid]

# ================= FFmpeg 工具 =================
def get_subprocess_kwargs():
    kwargs = dict(
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs

def get_duration(file_path, ffprobe_path):
    try:
        cmd = [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)]
        r = subprocess.run(cmd, **get_subprocess_kwargs())
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception as e:
        log(f"获取时长失败 {file_path}: {e}")
    return None

def clear_directory(path):
    if not path.exists():
        return
    for item in path.iterdir():
        if item.is_file() or item.is_symlink():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
    log(f"🧹 已清空目录: {path}")


# ================= 交叉淡化合成（无缝音频） =================
def assemble_crossfade(video_path, wav_list, ffmpeg_path, output_path, overlap):
    """将有序的窗口音频做交叉淡化，再与原视频 mux（视频仅复制一次，避免反复重编码）。

    采用平衡二叉树合并：每次只把相邻两段做一次 acrossfade，深度仅 log2(N) 层，
    相比原先把 N 段串成一条线性 acrossfade 长链（78 个节点串接），时长与内存都有界，
    且每个原始边界都只交叉淡化一次，效果与线性链逐边界完全一致。

    重要：ffmpeg 的 acrossfade 在「第二段明显短于第一段」时会输出错误长度，
    因此当第二段较短时，先用 apad 把第二段补长到与第一段等长，crossfade 后再
    atrim 回精确时长，从而既保住时间顺序又绕开该 ffmpeg bug。
    """
    if not wav_list:
        return False
    n = len(wav_list)

    if n == 1:
        cmd = [ffmpeg_path, "-y", "-i", str(video_path), "-i", str(wav_list[0]),
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(output_path)]
        r = subprocess.run(cmd, **get_subprocess_kwargs())
        return r.returncode == 0

    ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")
    # 仅在此处对原始段做一次 ffprobe；合并过程中时长按解析公式推算，
    # 不再反复探测临时文件，避免高频子进程拖慢与句柄争用。
    durs = [get_duration(w, ffprobe_path) or 0.0 for w in wav_list]
    items = [(Path(w), d) for w, d in zip(wav_list, durs)]

    tmp_root = output_path.parent / (output_path.stem + "_cf_tmp")
    try:
        tmp_root.mkdir(parents=True, exist_ok=True)
        level = 0
        while len(items) > 1:
            merged = []
            for i in range(0, len(items), 2):
                a, da = items[i]
                if i + 1 < len(items):
                    b, db = items[i + 1]
                    # 交叉淡化时长（保证不超过任一段可用长度）
                    d = overlap
                    if min(da, db) <= d + 0.05:
                        d = max(0.1, min(da, db) - 0.1)
                    if d < 0.15:
                        # 退化：直接拼接（极少出现）
                        flt = "[0:a:0][1:a:0]concat=v=0:a=1[out]"
                        nd = da + db
                    else:
                        if db < da:
                            # 第二段较短会触发 ffmpeg acrossfade 长度错误：
                            # 先把第二段补长到与第一段等长，结束后裁回精确时长。
                            flt = (f"[1:a:0]apad=whole_dur={da:.3f}[bpad];"
                                   f"[0:a:0][bpad]acrossfade=d={d:.3f}:curve=cos[out];"
                                   f"[out]atrim=0:{da + db - d:.3f}[out]")
                        else:
                            flt = f"[0:a:0][1:a:0]acrossfade=d={d:.3f}:curve=cos[out]"
                        nd = da + db - d
                    outp = tmp_root / f"m{level}_{i // 2:03d}.wav"
                    cmd = [ffmpeg_path, "-y", "-i", str(a), "-i", str(b),
                           "-filter_complex", flt,
                           "-map", "[out]", "-c:a", "pcm_s16le", str(outp)]
                    r = subprocess.run(cmd, **get_subprocess_kwargs())
                    if r.returncode != 0:
                        log(f"❌ 交叉淡化合并失败: {a.name} + {b.name}")
                        return False
                    merged.append((outp, nd))
                else:
                    merged.append((a, da))  # 奇数个时落单的一段直接进位
            items = merged
            level += 1
            log(f"🔗 交叉淡化合并 第 {level} 层，剩余 {len(items)} 段")

        final_audio, final_dur = items[0]
        # ---- 与原视频 mux（视频仅复制一次）----
        # 用解析推算的精确时长 -t 截断，避免部分 ffmpeg 下 -shortest 行为异常导致时长错乱。
        cmd = [ffmpeg_path, "-y", "-i", str(video_path), "-i", str(final_audio),
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-map", "0:v:0", "-map", "1:a:0", "-t", f"{final_dur:.3f}", str(output_path)]
        r = subprocess.run(cmd, **get_subprocess_kwargs())
        if r.returncode != 0:
            log(f"交叉淡化合成失败:\n{r.stderr}")
            return False
        return True
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

# ================= 视频分割（重叠窗口，优先流复制） =================
def split_overlap_windows(input_path, window, overlap, ffmpeg_path, output_dir):
    """按重叠窗口切割长视频：窗口=window 秒，相邻窗口重叠 overlap 秒。
    优先用流复制(-c copy)避免逐窗口重编码：起始点吸附到关键帧，保证切割
    落在关键帧上且窗口递进、彼此重叠。若源关键帧过疏无法保证重叠，则先
    一次性转码为“密集关键帧”中间文件（仅一次），再流复制切割。
    返回有序的窗口视频路径列表（命名 win_000.mp4 ...）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")
    total = get_duration(input_path, ffprobe_path)
    if total is None:
        log("❌ 无法获取视频时长，放弃切割")
        return None

    step = window - overlap
    if step <= 0:
        step = max(0.5, window / 2.0)

    # ---- 探测关键帧时间戳 ----
    def get_keyframes(path):
        try:
            cmd = [ffprobe_path, "-v", "error",
                   "-select_streams", "v:0",
                   "-show_entries", "frame=pts_time",
                   "-skip_frame", "nokey",
                   "-of", "csv=p=0", str(path)]
            r = subprocess.run(cmd, **get_subprocess_kwargs())
            if r.returncode == 0 and r.stdout.strip():
                return sorted(float(x) for x in r.stdout.strip().splitlines() if x.strip())
        except Exception as e:
            log(f"关键帧探测失败: {e}")
        return []

    def is_dense(kfs):
        """相邻关键帧间隔 ≤ step 时，吸附后能产出递进且重叠的窗口"""
        if len(kfs) < 2:
            return False
        return max(kfs[i+1] - kfs[i] for i in range(len(kfs)-1)) <= step + 1e-3

    src_for_cut = input_path
    keyframes = get_keyframes(input_path)
    force_reencode = False

    if not is_dense(keyframes):
        # 源关键帧过疏：一次性转码为密集关键帧（每 0.5s 一个）中间文件，仅做一次
        tmp_src = output_dir / "_keyed.mp4"
        log(f"⚠️ 源关键帧间隔过大，先转码为密集关键帧中间文件（仅一次，避免逐窗口重编码）")
        cmd = [ffmpeg_path, "-y", "-i", str(input_path),
               "-c:v", "libx264", "-crf", "18", "-preset", "fast",
               "-force_key_frames", "expr:gte(t,n_forced*0.5)",
               "-c:a", "copy", str(tmp_src)]
        r = subprocess.run(cmd, **get_subprocess_kwargs())
        if r.returncode != 0:
            log(f"❌ 密集关键帧转码失败，回退为逐窗口重编码:\n{r.stderr}")
            force_reencode = True
        else:
            keyframes = get_keyframes(tmp_src)
            if not is_dense(keyframes):
                # 极端情况：转码后仍不密集，直接逐窗口重编码
                force_reencode = True
            else:
                src_for_cut = tmp_src
                log(f"✅ 密集关键帧中间文件已生成: {tmp_src.name}")

    clips = []
    start = 0.0
    idx = 0
    while start < total - 0.05:
        end = min(start + window, total)
        out = output_dir / f"win_{idx:03d}.mp4"
        if force_reencode:
            cmd = [ffmpeg_path, "-y",
                   "-ss", f"{start:.3f}", "-i", str(src_for_cut),
                   "-t", f"{end - start:.3f}",
                   "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                   "-c:a", "copy", str(out)]
        else:
            # 吸附起始点到 ≤ start 的最近关键帧，保证流复制切割落在关键帧
            snap = start
            if keyframes:
                cand = [k for k in keyframes if k <= start + 1e-3]
                snap = cand[-1] if cand else 0.0
            cmd = [ffmpeg_path, "-y",
                   "-ss", f"{snap:.3f}", "-i", str(src_for_cut),
                   "-t", f"{end - snap:.3f}",
                   "-c", "copy", "-avoid_negative_ts", "make_zero",
                   str(out)]
        r = subprocess.run(cmd, **get_subprocess_kwargs())
        if r.returncode != 0:
            if not force_reencode:
                # 流复制偶发失败，回退该段重编码
                log(f"⚠️ 流复制切割窗口 {idx} 失败，回退重编码")
                cmd = [ffmpeg_path, "-y",
                       "-ss", f"{start:.3f}", "-i", str(src_for_cut),
                       "-t", f"{end - start:.3f}",
                       "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                       "-c:a", "copy", str(out)]
                r = subprocess.run(cmd, **get_subprocess_kwargs())
            if r.returncode != 0:
                log(f"❌ 切割窗口 {idx} 失败:\n{r.stderr}")
                return None
        clips.append(out)
        if end >= total:
            break
        start += step
        idx += 1

    mode = "逐窗口重编码" if force_reencode else "流复制"
    log(f"✅ 重叠窗口切割完成，共 {len(clips)} 段 (窗口 {window}s / 重叠 {overlap}s / 模式: {mode})")
    return clips

# ================= 核心处理函数（失败跳过） =================
def _move_to_failed(clip, safe_folder):
    """将原片段复制到 safe_folder/failed/ 下"""
    failed_dir = safe_folder / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = failed_dir / clip.name
    try:
        shutil.copy2(str(clip), str(dest))
        log(f"📋 失败片段已复制到 failed: {clip.name}")
    except Exception as e:
        log(f"复制失败片段出错: {e}")

def process_single_video(video_path, config, in_progress_state=None):
    comfy_url = config["comfy_url"]
    workflow_path = config["workflow_path"]
    output_dir = config["output_dir"]
    temp_dir = config["temp_dir"]
    ffmpeg_path = config["ffmpeg_path"]
    clear_output = config.get("clear_output", True)
    clear_temp = config.get("clear_temp", True)
    clear_segments = config.get("clear_segments", True)

    log(f"📼 开始处理: {video_path.name}")

    if in_progress_state:
        segment_dir = SEGMENTS_BASE / in_progress_state["segment_dir"]
        if not segment_dir.exists():
            log(f"❌ 片段目录 {segment_dir} 不存在，重新切割")
            in_progress_state = None
        else:
            clips = sorted(segment_dir.glob("win_*.mp4"))
            log(f"🔄 从断点恢复，已切割片段 {len(clips)} 个")
    else:
        safe_name = video_path.stem
        segment_dir = SEGMENTS_BASE / safe_name
        if segment_dir.exists():
            clear_directory(segment_dir)
        clips = split_overlap_windows(video_path, SEGMENT_WINDOW, OVERLAP, ffmpeg_path, segment_dir)
        if not clips:
            return None, None

    if not in_progress_state:
        in_progress_state = {
            "segment_dir": video_path.stem,
            "total_segments": len(clips),
            "processed": 0,
            "generated_files": []
        }

    try:
        prompt_template = load_api_workflow(workflow_path)
    except Exception as e:
        log_exception("加载工作流失败")
        return None, None

    load_id = combine_id = None
    for nid, ndata in prompt_template.items():
        ct = ndata.get("class_type", "")
        if ct == "VHS_LoadVideoPath":
            load_id = nid
        elif ct == "VHS_VideoCombine":
            combine_id = nid
    if not load_id or not combine_id:
        log("❌ 缺少必要节点")
        return None, None

    if "save_output" in prompt_template[combine_id]["inputs"]:
        prompt_template[combine_id]["inputs"]["save_output"] = True

    safe_folder = FINISH_DIR / video_path.stem
    safe_folder.mkdir(parents=True, exist_ok=True)

    generated = []
    processed = in_progress_state["processed"]
    for old_file in in_progress_state.get("generated_files", []):
        path = safe_folder / old_file
        if path.exists():
            generated.append(path)

    total = len(clips)

    # ----- 内部函数：处理单个窗口视频，提取音频为 WAV，返回 WAV 路径，失败返回 None -----
    def _process_one_clip(clip_path, win_idx=None):
        nonlocal prompt_template, load_id, combine_id, comfy_url, output_dir, temp_dir, ffmpeg_path, safe_folder

        prefix = f"MMaudio_win_{win_idx:03d}" if win_idx is not None else f"MMaudio_{uuid.uuid4().hex[:6]}"
        prompt_template[load_id]["inputs"]["video"] = str(clip_path)
        prompt_template[combine_id]["inputs"]["filename_prefix"] = prefix

        pid = submit_prompt(comfy_url, prompt_template)
        if not pid:
            log(f"⚠️ 提交失败: {Path(clip_path).name}")
            return None

        wait_for_prompt(comfy_url, pid)

        out_files = []
        for directory in (output_dir, temp_dir):
            path = Path(directory)
            if not path.exists():
                continue
            for f in path.glob(f"{prefix}_*-audio.mp4"):
                out_files.append(f)

        if not out_files:
            log(f"⚠️ 未找到输出文件: {prefix}_*-audio.mp4")
            return None

        src = out_files[0]
        wav_path = safe_folder / f"{prefix}.wav"
        cmd_wav = [
            ffmpeg_path, "-y",
            "-i", str(src),
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100",
            str(wav_path)
        ]
        r_wav = subprocess.run(cmd_wav, **get_subprocess_kwargs())
        if r_wav.returncode != 0:
            log(f"⚠️ 音频提取失败: {r_wav.stderr}")
            return None

        try:
            shutil.move(str(src), str(safe_folder / src.name))
        except Exception as e:
            log(f"归档原始输出失败 {src.name}: {e}")

        log(f"✅ 窗口音频提取完成: {wav_path.name}")
        return wav_path

    # ----- 重叠窗口循环处理 -----
    win_idx = len(generated)
    idx = processed
    while idx < len(clips):
        if stop_flag.is_set():
            log("⏹️ 停止信号，保存进度")
            break

        clip = clips[idx]
        log(f"▶️ 窗口 {idx+1}/{len(clips)}: {clip.name} (音频序号 {win_idx})")

        out = _process_one_clip(clip, win_idx=win_idx)
        if out is not None:
            generated.append(out)
            idx += 1
            win_idx += 1
            in_progress_state["processed"] = idx
            in_progress_state["generated_files"] = [p.name for p in generated]
            save_progress(completed_videos, in_progress_all)
            continue

        # --- 失败：对半切割后放回列表，下一轮继续处理（保持时间顺序）---
        ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")
        dur = get_duration(clip, ffprobe_path)
        if dur is None or dur < 3.0 or "_retry" in clip.name:
            log(f"❌ 窗口 {clip.name} 处理失败且无法再分割，放弃该视频")
            _move_to_failed(clip, safe_folder)
            if clear_segments:
                clear_directory(segment_dir)
                segment_dir.rmdir()
                log("🗑️ 已删除持久化片段目录")
            return None, None

        log(f"🔄 窗口 {clip.name} 处理失败，对半切割后重试...")
        half = dur / 2.0
        part1 = clip.parent / f"{clip.stem}_retry_a{clip.suffix}"
        part2 = clip.parent / f"{clip.stem}_retry_b{clip.suffix}"

        r1 = subprocess.run([
            ffmpeg_path, "-y", "-i", str(clip), "-ss", "0", "-t", str(half),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-c:a", "copy", str(part1)
        ], **get_subprocess_kwargs())
        r2 = subprocess.run([
            ffmpeg_path, "-y", "-i", str(clip), "-ss", str(half), "-t", str(half),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-c:a", "copy", str(part2)
        ], **get_subprocess_kwargs())
        if r1.returncode != 0 or r2.returncode != 0:
            log(f"❌ 切割重试失败，放弃该视频")
            for p in (part1, part2):
                if p.exists():
                    p.unlink(missing_ok=True)
            _move_to_failed(clip, safe_folder)
            if clear_segments:
                clear_directory(segment_dir)
                segment_dir.rmdir()
                log("🗑️ 已删除持久化片段目录")
            return None, None

        clip.unlink(missing_ok=True)
        clips[idx:idx+1] = [part1, part2]
        log(f"✂️ 已替换为 {part1.name} 与 {part2.name}，继续处理")

    if stop_flag.is_set() or idx < len(clips):
        log(f"⏸️ 暂停在窗口 {idx}/{len(clips)}，进度已保存")
        return None, in_progress_state

    # ---------- 合成阶段（重叠窗口 + 音频交叉淡化，无缝过渡） ----------
    log(f"🔗 交叉淡化合成 {len(generated)} 个窗口音频...")
    if not generated:
        log("❌ 无输出片段可合成")
        if clear_segments:
            clear_directory(segment_dir)
            segment_dir.rmdir()
            log("🗑️ 已删除持久化片段目录")
        return None, None

    def extract_num(fpath):
        m = re.search(r'(\d+)', Path(fpath).stem)
        return int(m.group(1)) if m else 0

    generated.sort(key=extract_num)

    final = FINISH_DIR / f"{video_path.stem}.mp4"
    ok = assemble_crossfade(video_path, generated, ffmpeg_path, final, OVERLAP)

    # 根据配置清理输出/临时目录
    if clear_output:
        clear_directory(Path(output_dir))
    if clear_temp:
        clear_directory(Path(temp_dir))

    if clear_segments:
        clear_directory(segment_dir)
        segment_dir.rmdir()
        log("🗑️ 已删除持久化片段目录")

    if not ok:
        log("❌ 合成失败")
        return None, None

    log(f"🎉 合成成功: {final.name}")
    return final, None

# ================= 手动合成功能（交叉淡化版） =================
def manual_merge(video_name, ffmpeg_path):
    safe_folder = FINISH_DIR / video_name
    if not safe_folder.exists() or not safe_folder.is_dir():
        log(f"❌ 目录 {safe_folder} 不存在")
        return False

    clips = list(safe_folder.glob("MMaudio_win_*.wav"))
    if not clips:
        log(f"❌ 目录 {safe_folder} 中没有找到窗口音频 MMaudio_win_*.wav")
        return False

    def extract_num(fpath):
        m = re.search(r'(\d+)', Path(fpath).stem)
        return int(m.group(1)) if m else 0

    clips.sort(key=extract_num)

    original_video = SCRIPT_DIR / f"{video_name}.mp4"
    if not original_video.exists():
        log(f"❌ 找不到原视频 {original_video}，无法合成")
        return False

    output_path = FINISH_DIR / f"{video_name}.mp4"
    if assemble_crossfade(original_video, clips, ffmpeg_path, output_path, OVERLAP):
        log(f"🎉 手动合成成功: {output_path.name}")
        try:
            shutil.move(str(original_video), str(DUCE_DIR / original_video.name))
            log(f"📁 原视频已移至 Duce")
        except Exception as e:
            log(f"移动原视频失败: {e}")
        return True
    else:
        log(f"手动合成失败")
        return False

# ================= GUI 应用 =================
class VideoProcessorApp(Tk):
    def __init__(self):
        super().__init__()
        self.title("MMAudio 批量处理 v4.0（重叠窗口·交叉淡化无缝版）")
        self.geometry("1000x800")
        self.resizable(True, True)
        self.configure(bg='#f0f0f0')

        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TButton', padding=6, relief="flat", background="#4a7a8c")
        style.map('TButton', background=[('active', '#5d8a9e')])
        style.configure('TLabel', background='#f0f0f0')
        style.configure('TFrame', background='#f0f0f0')
        style.configure('Header.TLabel', font=('微软雅黑', 10, 'bold'))
        style.configure('TCheckbutton', background='#f0f0f0')

        self.config = load_config()
        self.completed_videos, self.in_progress = load_progress()
        self.video_files = find_videos()
        self.running = False
        self.stop_requested = False
        self.current_thread = None
        self._loading = True

        self.clear_output_var = BooleanVar(value=self.config.get("clear_output", True))
        self.clear_temp_var = BooleanVar(value=self.config.get("clear_temp", True))
        self.clear_segments_var = BooleanVar(value=self.config.get("clear_segments", True))

        self.create_widgets()
        self._setup_auto_save()
        self._loading = False
        self.refresh_list()
        self.refresh_incomplete_list()
        self.after(100, self.poll_log_queue)

    def _setup_auto_save(self):
        def auto_save_callback(*args):
            if not self._loading:
                self._save_config_from_vars()
        self.comfy_url_var.trace_add('write', auto_save_callback)
        self.workflow_path_var.trace_add('write', auto_save_callback)
        self.output_dir_var.trace_add('write', auto_save_callback)
        self.temp_dir_var.trace_add('write', auto_save_callback)
        self.ffmpeg_path_var.trace_add('write', auto_save_callback)
        self.clear_output_var.trace_add('write', auto_save_callback)
        self.clear_temp_var.trace_add('write', auto_save_callback)
        self.clear_segments_var.trace_add('write', auto_save_callback)

    def _save_config_from_vars(self):
        new_config = {
            "comfy_url": self.comfy_url_var.get(),
            "workflow_path": self.workflow_path_var.get(),
            "output_dir": self.output_dir_var.get(),
            "temp_dir": self.temp_dir_var.get(),
            "ffmpeg_path": self.ffmpeg_path_var.get(),
            "clear_output": self.clear_output_var.get(),
            "clear_temp": self.clear_temp_var.get(),
            "clear_segments": self.clear_segments_var.get()
        }
        save_config(new_config)
        self.config = new_config

    def _open_folder(self, path_var):
        path = path_var.get().strip()
        if not path:
            messagebox.showwarning("路径为空", "请先设置路径")
            return
        p = Path(path)
        if p.is_file():
            folder = p.parent
        else:
            folder = p
        if not folder.exists():
            messagebox.showwarning("路径不存在", f"路径不存在: {folder}")
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            messagebox.showerror("打开失败", f"无法打开文件夹:\n{e}")

    def create_widgets(self):
        config_frame = ttk.LabelFrame(self, text=" 设置 ", padding=10)
        config_frame.pack(fill=X, padx=10, pady=10)

        ttk.Label(config_frame, text="ComfyUI 地址:", style='Header.TLabel').grid(row=0, column=0, sticky=W, padx=5, pady=5)
        self.comfy_url_var = StringVar(value=self.config["comfy_url"])
        ttk.Entry(config_frame, textvariable=self.comfy_url_var, width=40).grid(row=0, column=1, sticky=W, padx=5)

        ttk.Label(config_frame, text="API 工作流:", style='Header.TLabel').grid(row=1, column=0, sticky=W, padx=5, pady=5)
        self.workflow_path_var = StringVar(value=self.config["workflow_path"])
        ttk.Entry(config_frame, textvariable=self.workflow_path_var, width=50).grid(row=1, column=1, sticky=W, padx=5)
        ttk.Button(config_frame, text="浏览...", command=self.browse_workflow).grid(row=1, column=2, padx=5)
        ttk.Button(config_frame, text="📂", width=3, command=lambda: self._open_folder(self.workflow_path_var)).grid(row=1, column=3, padx=2)

        ttk.Label(config_frame, text="Output 目录:", style='Header.TLabel').grid(row=2, column=0, sticky=W, padx=5, pady=5)
        self.output_dir_var = StringVar(value=self.config["output_dir"])
        ttk.Entry(config_frame, textvariable=self.output_dir_var, width=50).grid(row=2, column=1, sticky=W, padx=5)
        ttk.Button(config_frame, text="浏览...", command=self.browse_output).grid(row=2, column=2, padx=5)
        ttk.Button(config_frame, text="📂", width=3, command=lambda: self._open_folder(self.output_dir_var)).grid(row=2, column=3, padx=2)

        ttk.Label(config_frame, text="Temp 目录:", style='Header.TLabel').grid(row=3, column=0, sticky=W, padx=5, pady=5)
        self.temp_dir_var = StringVar(value=self.config["temp_dir"])
        ttk.Entry(config_frame, textvariable=self.temp_dir_var, width=50).grid(row=3, column=1, sticky=W, padx=5)
        ttk.Button(config_frame, text="浏览...", command=self.browse_temp).grid(row=3, column=2, padx=5)
        ttk.Button(config_frame, text="📂", width=3, command=lambda: self._open_folder(self.temp_dir_var)).grid(row=3, column=3, padx=2)

        ttk.Label(config_frame, text="FFmpeg:", style='Header.TLabel').grid(row=4, column=0, sticky=W, padx=5, pady=5)
        self.ffmpeg_path_var = StringVar(value=self.config["ffmpeg_path"])
        ttk.Entry(config_frame, textvariable=self.ffmpeg_path_var, width=40).grid(row=4, column=1, sticky=W, padx=5)
        ttk.Button(config_frame, text="浏览...", command=self.browse_ffmpeg).grid(row=4, column=2, padx=5)
        ttk.Button(config_frame, text="📂", width=3, command=lambda: self._open_folder(self.ffmpeg_path_var)).grid(row=4, column=3, padx=2)

        ttk.Label(config_frame, text="合成后清空:", style='Header.TLabel').grid(row=5, column=0, sticky=W, padx=5, pady=10)
        switch_frame = ttk.Frame(config_frame)
        switch_frame.grid(row=5, column=1, sticky=W, padx=5)
        ttk.Checkbutton(switch_frame, text="output", variable=self.clear_output_var).pack(side=LEFT, padx=5)
        ttk.Checkbutton(switch_frame, text="temp", variable=self.clear_temp_var).pack(side=LEFT, padx=5)
        ttk.Checkbutton(switch_frame, text="segments", variable=self.clear_segments_var).pack(side=LEFT, padx=5)

        control_frame = ttk.Frame(self)
        control_frame.pack(fill=X, padx=10, pady=5)

        self.start_btn = ttk.Button(control_frame, text="▶ 启动", command=self.start_processing)
        self.start_btn.pack(side=LEFT, padx=5)
        self.stop_btn = ttk.Button(control_frame, text="⏹️ 停止", command=self.request_stop, state="disabled")
        self.stop_btn.pack(side=LEFT, padx=5)
        ttk.Button(control_frame, text="🔄 刷新列表", command=self.refresh_list).pack(side=LEFT, padx=5)

        self.progress_var = StringVar(value="就绪")
        self.progress_bar = Progressbar(control_frame, orient="horizontal", mode="indeterminate")
        self.progress_bar.pack(side=RIGHT, padx=10, fill=X, expand=True)

        main_frame = ttk.Frame(self)
        main_frame.pack(fill=BOTH, expand=True, padx=10, pady=5)

        list_frame = ttk.LabelFrame(main_frame, text=" 待处理视频 ", padding=5)
        list_frame.pack(side=LEFT, fill=BOTH, expand=True)

        self.listbox = Listbox(list_frame, selectmode="extended", font=("微软雅黑", 9), bg="white")
        self.listbox.pack(side=LEFT, fill=BOTH, expand=True)
        list_scroll = ttk.Scrollbar(list_frame, orient=VERTICAL, command=self.listbox.yview)
        list_scroll.pack(side=RIGHT, fill=Y)
        self.listbox.config(yscrollcommand=list_scroll.set)

        incomplete_frame = ttk.LabelFrame(main_frame, text=" 未合成视频 (可手动合成) ", padding=5)
        incomplete_frame.pack(side=LEFT, fill=BOTH, expand=True, padx=(5,0))

        self.incomplete_listbox = Listbox(incomplete_frame, selectmode=SINGLE, font=("微软雅黑", 9), bg="white")
        self.incomplete_listbox.pack(fill=BOTH, expand=True)
        btn_merge = ttk.Button(incomplete_frame, text="🔧 合成选中视频", command=self.manual_merge_selected)
        btn_merge.pack(pady=5)

        log_frame = ttk.LabelFrame(main_frame, text=" 实时日志 ", padding=5)
        log_frame.pack(side=RIGHT, fill=BOTH, expand=True)

        self.log_text = Text(log_frame, wrap="word", state="normal", font=("Consolas", 9), bg="white")
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient=VERTICAL, command=self.log_text.yview)
        log_scroll.pack(side=RIGHT, fill=Y)
        self.log_text.config(yscrollcommand=log_scroll.set)

        self.status_var = StringVar(value="就绪 - 等待操作")
        status_bar = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor=W, padding=5)
        status_bar.pack(side="bottom", fill=X)

    def refresh_incomplete_list(self):
        self.incomplete_listbox.delete(0, END)
        if not FINISH_DIR.exists():
            return
        for item in FINISH_DIR.iterdir():
            if item.is_dir():
                name = item.name
                expected_video = FINISH_DIR / f"{name}.mp4"
                if not expected_video.exists():
                    self.incomplete_listbox.insert(END, name)

    def manual_merge_selected(self):
        selection = self.incomplete_listbox.curselection()
        if not selection:
            messagebox.showwarning("提示", "请先选择一个未合成的视频")
            return
        video_name = self.incomplete_listbox.get(selection[0])
        ffmpeg = self.config["ffmpeg_path"]
        if not video_name:
            return
        log(f"🔧 手动合成请求: {video_name}")
        threading.Thread(target=self._manual_merge_thread, args=(video_name, ffmpeg), daemon=True).start()

    def _manual_merge_thread(self, video_name, ffmpeg_path):
        success = manual_merge(video_name, ffmpeg_path)
        if success:
            if video_name not in self.completed_videos:
                self.completed_videos.append(video_name)
                save_progress(self.completed_videos, self.in_progress)
            self.after(0, self.refresh_incomplete_list)
            self.after(0, self.refresh_list)
        else:
            log(f"❌ 手动合成失败: {video_name}")

    def browse_workflow(self):
        path = filedialog.askopenfilename(title="选择工作流文件", filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if path:
            self.workflow_path_var.set(path)

    def browse_output(self):
        path = filedialog.askdirectory(title="选择 Output 目录")
        if path:
            self.output_dir_var.set(path)

    def browse_temp(self):
        path = filedialog.askdirectory(title="选择 Temp 目录")
        if path:
            self.temp_dir_var.set(path)

    def browse_ffmpeg(self):
        path = filedialog.askopenfilename(title="选择 ffmpeg 可执行文件", filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if path:
            self.ffmpeg_path_var.set(path)

    def refresh_list(self):
        self.video_files = find_videos()
        self.listbox.delete(0, END)
        for vf in self.video_files:
            status = " ✅" if vf.name in self.completed_videos else ""
            self.listbox.insert(END, f"{vf.name}{status}")
        self.refresh_incomplete_list()

    def log_to_gui(self, line):
        self.log_text.insert(END, line + "\n")
        self.log_text.see(END)

    def poll_log_queue(self):
        while not log_queue.empty():
            line = log_queue.get()
            self.log_to_gui(line)
        self.after(100, self.poll_log_queue)

    def set_buttons_state(self, running):
        if running:
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.progress_bar.start(10)
        else:
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.progress_bar.stop()

    def start_processing(self):
        if self.running:
            return
        self.config = {
            "comfy_url": self.comfy_url_var.get(),
            "workflow_path": self.workflow_path_var.get(),
            "output_dir": self.output_dir_var.get(),
            "temp_dir": self.temp_dir_var.get(),
            "ffmpeg_path": self.ffmpeg_path_var.get(),
            "clear_output": self.clear_output_var.get(),
            "clear_temp": self.clear_temp_var.get(),
            "clear_segments": self.clear_segments_var.get()
        }
        try:
            requests.get(self.config["comfy_url"] + "/system_stats", timeout=5)
        except Exception:
            messagebox.showerror("连接错误", "无法连接到 ComfyUI")
            return

        FINISH_DIR.mkdir(exist_ok=True)
        DUCE_DIR.mkdir(exist_ok=True)

        self.running = True
        self.stop_requested = False
        global stop_flag
        stop_flag.clear()
        self.set_buttons_state(True)
        self.status_var.set("正在处理视频...")

        global completed_videos, in_progress_all
        completed_videos = self.completed_videos
        in_progress_all = self.in_progress

        self.current_thread = threading.Thread(target=self.processing_loop, daemon=True)
        self.current_thread.start()

    def request_stop(self):
        self.stop_requested = True
        global stop_flag
        stop_flag.set()
        self.status_var.set("正在停止...")
        self.stop_btn.config(state="disabled")

    def processing_loop(self):
        try:
            remaining = [vf for vf in self.video_files if vf.name not in self.completed_videos]
            total = len(remaining)
            for idx, video_path in enumerate(remaining):
                if self.stop_requested:
                    break
                log("=" * 40)
                log(f"处理视频 {idx+1}/{total}: {video_path.name}")

                in_progress_state = self.in_progress.get(video_path.name)
                final_video, updated_state = process_single_video(video_path, self.config, in_progress_state)

                if final_video is None:
                    if updated_state is not None:
                        self.in_progress[video_path.name] = updated_state
                        save_progress(self.completed_videos, self.in_progress)
                    else:
                        if video_path.name in self.in_progress:
                            del self.in_progress[video_path.name]
                            save_progress(self.completed_videos, self.in_progress)
                    continue
                else:
                    log(f"🎉 最终视频: {final_video.name}")
                    try:
                        dest_original = DUCE_DIR / video_path.name
                        if video_path.exists():
                            shutil.move(str(video_path), str(dest_original))
                            log(f"📁 原视频已移至 Duce")
                    except Exception as e:
                        log(f"移动原视频出错: {e}")
                    self.completed_videos.append(video_path.name)
                    if video_path.name in self.in_progress:
                        del self.in_progress[video_path.name]
                    save_progress(self.completed_videos, self.in_progress)
                    self.after(0, self.refresh_list)

            if not self.stop_requested:
                log("🏁 所有视频处理完成！")
                self.after(0, lambda: self.status_var.set("全部完成"))
        except Exception as e:
            log_exception("处理线程异常")
        finally:
            self.running = False
            self.stop_requested = False
            self.after(0, lambda: self.set_buttons_state(False))
            self.after(0, lambda: self.status_var.set("就绪"))
            self.after(0, self.refresh_incomplete_list)

# 全局变量
completed_videos = []
in_progress_all = {}
stop_flag = threading.Event()

if __name__ == "__main__":
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("")
    app = VideoProcessorApp()
    app.mainloop()