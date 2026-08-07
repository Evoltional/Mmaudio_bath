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
    "workflow_path": r"D:\Order\DeskTop\KF\Me-Python\Mmaudio_Bath\mmaudio_NSFW_bath.json",
    "output_dir": r"E:\Ordrnary\ComfyUI-aki-v3\ComfyUI\output",
    "temp_dir": r"E:\Ordrnary\ComfyUI-aki-v3\ComfyUI\temp",
    "ffmpeg_path": "ffmpeg",
    "clear_output": True,
    "clear_temp": True,
    "clear_segments": True
}
SEGMENT_DURATION = 5
MAX_SEGMENT_DURATION = 9          # 超过则对半切割
MIN_MERGE_DURATION = 5            # 小于此值尝试合并
MAX_MERGED_DURATION = 9           # 合并后总时长上限
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

# ================= 合并短视频 =================
def _merge_two_clips(clip1, clip2, ffmpeg_path, output_dir):
    merged_name = f"{clip1.stem}_m{clip2.stem}{clip1.suffix}"
    merged_path = output_dir / merged_name
    cmd = [
        ffmpeg_path, "-y",
        "-i", str(clip1),
        "-i", str(clip2),
        "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]",
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        str(merged_path)
    ]
    r = subprocess.run(cmd, **get_subprocess_kwargs())
    if r.returncode != 0:
        log(f"合并 {clip1.name} 与 {clip2.name} 失败:\n{r.stderr}")
        return None
    log(f"🔀 合并: {clip1.name} + {clip2.name} -> {merged_name}")
    return merged_path

def merge_short_clips(clips, ffmpeg_path, ffprobe_path, output_dir):
    def is_half_segment(clip):
        stem = clip.stem
        return stem[-1] in 'ab' and stem[-2:].isdigit()

    def can_merge(clip):
        return not is_half_segment(clip)

    clip_info = []
    for c in clips:
        dur = get_duration(c, ffprobe_path)
        if dur is None:
            dur = 0
        clip_info.append({"path": c, "duration": dur, "half": is_half_segment(c)})

    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(clip_info):
            cur = clip_info[i]
            if cur["duration"] >= MIN_MERGE_DURATION or not can_merge(cur["path"]):
                i += 1
                continue
            prev = clip_info[i-1] if i > 0 else None
            next_ = clip_info[i+1] if i < len(clip_info)-1 else None

            candidates = []
            if prev and can_merge(prev["path"]):
                candidates.append(prev)
            if next_ and can_merge(next_["path"]):
                candidates.append(next_)
            if not candidates:
                i += 1
                continue

            target = min(candidates, key=lambda x: x["duration"])
            combined_duration = cur["duration"] + target["duration"]
            if combined_duration >= MAX_MERGED_DURATION:
                i += 1
                continue

            new_path = _merge_two_clips(cur["path"], target["path"], ffmpeg_path, output_dir)
            if new_path is None:
                i += 1
                continue

            try:
                cur["path"].unlink(missing_ok=True)
                target["path"].unlink(missing_ok=True)
            except:
                pass

            if target == prev:
                del clip_info[i]
                del clip_info[i-1]
                new_dur = get_duration(new_path, ffprobe_path) or combined_duration
                clip_info.insert(i-1, {"path": new_path, "duration": new_dur, "half": False})
                i -= 1
            else:
                del clip_info[i+1]
                del clip_info[i]
                new_dur = get_duration(new_path, ffprobe_path) or combined_duration
                clip_info.insert(i, {"path": new_path, "duration": new_dur, "half": False})
            changed = True
    return [info["path"] for info in clip_info]

# ================= 视频分割 =================
def split_video(input_path, dur, ffmpeg_path, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / "seg_%03d.mp4")
    safe_input = output_dir / f"input_{uuid.uuid4().hex}.mp4"
    try:
        shutil.copy2(str(input_path), str(safe_input))
    except Exception as e:
        log(f"❌ 复制视频失败: {e}")
        return None

    cmd = [
        ffmpeg_path,
        "-i", str(safe_input),
        "-c", "copy",
        "-map", "0:v",
        "-map", "0:a?",
        "-dn",
        "-segment_time", str(dur),
        "-f", "segment",
        "-reset_timestamps", "1",
        pattern
    ]
    r = subprocess.run(cmd, **get_subprocess_kwargs())
    safe_input.unlink(missing_ok=True)
    if r.returncode != 0:
        err_msg = r.stderr.strip() or r.stdout.strip() or "未知错误"
        log(f"初次分割失败:\n{err_msg}")
        return None

    clips = sorted(output_dir.glob("seg_*.mp4"))
    log(f"✅ 初次分割完成，共 {len(clips)} 段")

    ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")
    processed_clips = []
    for clip in clips:
        duration = get_duration(clip, ffprobe_path)
        if duration is None:
            log(f"⚠️ 无法获取 {clip.name} 时长，保留原片段")
            processed_clips.append(clip)
            continue

        if duration < MAX_SEGMENT_DURATION:
            processed_clips.append(clip)
        else:
            log(f"⏳ {clip.name} 时长 {duration:.1f}s，超过 {MAX_SEGMENT_DURATION}s，对半重编码切割...")
            half = duration / 2.0
            part1 = clip.parent / f"{clip.stem}a{clip.suffix}"
            part2 = clip.parent / f"{clip.stem}b{clip.suffix}"

            cmd1 = [
                ffmpeg_path, "-y",
                "-i", str(clip),
                "-ss", "0", "-t", str(half),
                "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k",
                str(part1)
            ]
            cmd2 = [
                ffmpeg_path, "-y",
                "-i", str(clip),
                "-ss", str(half), "-t", str(half),
                "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k",
                str(part2)
            ]

            r1 = subprocess.run(cmd1, **get_subprocess_kwargs())
            r2 = subprocess.run(cmd2, **get_subprocess_kwargs())
            if r1.returncode == 0 and r2.returncode == 0:
                log(f"  切割成功: {part1.name} + {part2.name}")
                clip.unlink(missing_ok=True)
                processed_clips.append(part1)
                processed_clips.append(part2)
            else:
                log(f"  重编码切割失败，保留原片段 {clip.name}")
                if part1.exists():
                    part1.unlink(missing_ok=True)
                if part2.exists():
                    part2.unlink(missing_ok=True)
                processed_clips.append(clip)

    processed_clips = merge_short_clips(processed_clips, ffmpeg_path, ffprobe_path, output_dir)

    final_clips = sorted(processed_clips, key=lambda x: x.name)
    log(f"🔧 最终待处理片段数: {len(final_clips)}")
    return final_clips

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
            clips = sorted(segment_dir.glob("seg_*.mp4"))
            log(f"🔄 从断点恢复，已切割片段 {len(clips)} 个")
    else:
        safe_name = video_path.stem
        segment_dir = SEGMENTS_BASE / safe_name
        if segment_dir.exists():
            clear_directory(segment_dir)
        clips = split_video(video_path, SEGMENT_DURATION, ffmpeg_path, segment_dir)
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

    # ----- 内部函数：处理单个视频片段，返回生成的文件列表，失败返回 None -----
    def _process_one_clip(clip_path, seg_index=None):
        """
        提交片段到 ComfyUI，将输出文件移至 safe_folder 并完成音频同步。
        成功返回 [Path, ...]，失败返回 None。
        """
        nonlocal prompt_template, load_id, combine_id, comfy_url, output_dir, temp_dir, ffmpeg_path, safe_folder

        # 生成唯一前缀，避免文件冲突
        prefix = f"MMaudio_seg_{seg_index:03d}" if seg_index is not None else f"MMaudio_{uuid.uuid4().hex[:6]}"
        prompt_template[load_id]["inputs"]["video"] = str(clip_path)
        prompt_template[combine_id]["inputs"]["filename_prefix"] = prefix

        pid = submit_prompt(comfy_url, prompt_template)
        if not pid:
            log(f"⚠️ 提交失败: {Path(clip_path).name}")
            return None

        wait_for_prompt(comfy_url, pid)
        time.sleep(1)

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

        # 移动输出并同步音频
        result = []
        for f in out_files:
            dest = safe_folder / f.name
            try:
                shutil.move(str(f), str(dest))
            except Exception as e:
                log(f"移动输出文件失败 {f.name}: {e}")
                continue

            synced_tmp = dest.with_suffix('.sync.mp4')
            cmd_sync = [
                ffmpeg_path, "-y",
                "-i", str(clip_path),
                "-i", str(dest),
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v",
                "-map", "1:a",
                "-shortest",
                str(synced_tmp)
            ]
            r_sync = subprocess.run(cmd_sync, **get_subprocess_kwargs())
            if r_sync.returncode == 0:
                synced_tmp.replace(dest)
                log(f"✅ 音频同步完成: {dest.name}")
            else:
                log(f"⚠️ 音频同步失败，保留原始输出: {r_sync.stderr}")
                synced_tmp.unlink(missing_ok=True)
            result.append(dest)
        return result

    # ----- 片段循环处理 -----
    idx = processed
    while idx < total:
        if stop_flag.is_set():
            log("⏹️ 停止信号，保存进度")
            break

        clip = clips[idx]
        log(f"▶️ 片段 {idx+1}/{total}: {clip.name}")

        # 第一次尝试处理原片段
        out_files = _process_one_clip(clip, seg_index=idx)
        if out_files is not None:
            generated.extend(out_files)
            idx += 1
            processed = idx
            in_progress_state["processed"] = processed
            in_progress_state["generated_files"] = [p.name for p in generated]
            save_progress(completed_videos, in_progress_all)
            continue

        # --- 失败：尝试对半切割重试 ---
        log(f"🔄 片段 {clip.name} 处理失败，尝试对半切割重试...")
        ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")
        dur = get_duration(clip, ffprobe_path)
        if dur is None or dur < 2.0:
            log(f"❌ 片段时长过短或无法获取，放弃该视频")
            _move_to_failed(clip, safe_folder)
            # 清理并返回失败
            if clear_segments:
                clear_directory(segment_dir)
                segment_dir.rmdir()
                log("🗑️ 已删除持久化片段目录")
            return None, None

        half = dur / 2.0
        part1 = clip.parent / f"{clip.stem}_retry_a{clip.suffix}"
        part2 = clip.parent / f"{clip.stem}_retry_b{clip.suffix}"

        # 切割为两个子片段
        cmd_split1 = [
            ffmpeg_path, "-y",
            "-i", str(clip),
            "-ss", "0", "-t", str(half),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            str(part1)
        ]
        cmd_split2 = [
            ffmpeg_path, "-y",
            "-i", str(clip),
            "-ss", str(half), "-t", str(half),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            str(part2)
        ]
        r1 = subprocess.run(cmd_split1, **get_subprocess_kwargs())
        r2 = subprocess.run(cmd_split2, **get_subprocess_kwargs())
        if r1.returncode != 0 or r2.returncode != 0:
            log(f"❌ 切割重试失败，保留原片段，放弃该视频")
            # 清理临时片段
            for p in [part1, part2]:
                if p.exists():
                    p.unlink(missing_ok=True)
            _move_to_failed(clip, safe_folder)
            if clear_segments:
                clear_directory(segment_dir)
                segment_dir.rmdir()
                log("🗑️ 已删除持久化片段目录")
            return None, None

        log(f"✂️ 已切割为 {part1.name} 和 {part2.name}，逐一处理")
        # 处理子片段1
        out1 = _process_one_clip(part1, seg_index=idx)  # 使用同一 idx，文件名可能冲突，但前缀包含 idx 所以没问题
        if out1 is None:
            log(f"❌ 子片段 {part1.name} 处理失败，放弃该视频")
            for p in [part1, part2]:
                if p.exists():
                    p.unlink(missing_ok=True)
            _move_to_failed(clip, safe_folder)
            if clear_segments:
                clear_directory(segment_dir)
                segment_dir.rmdir()
                log("🗑️ 已删除持久化片段目录")
            return None, None

        # 处理子片段2
        out2 = _process_one_clip(part2, seg_index=idx+1)  # 这里 idx 可能造成前缀重复，后面会修正
        if out2 is None:
            log(f"❌ 子片段 {part2.name} 处理失败，放弃该视频")
            # 清理已生成的子片段输出文件（避免残留）
            for f in out1:
                if f.exists():
                    f.unlink(missing_ok=True)
            for p in [part1, part2]:
                if p.exists():
                    p.unlink(missing_ok=True)
            _move_to_failed(clip, safe_folder)
            if clear_segments:
                clear_directory(segment_dir)
                segment_dir.rmdir()
                log("🗑️ 已删除持久化片段目录")
            return None, None

        # 两个子片段都成功，替换原片段
        log(f"✅ 切割重试成功，子片段输出已保存")
        # 删除原片段
        clip.unlink(missing_ok=True)
        # 删除切割产生的临时子片段文件
        part1.unlink(missing_ok=True)
        part2.unlink(missing_ok=True)
        # 将输出文件加入 generated
        generated.extend(out1)
        generated.extend(out2)
        # 更新 processed 计数（这里占用了一个原片段位置，但变成了两个输出，后续处理继续）
        # 注意：切割后原片段被替换，processed 应 +1，但列表长度变化，这里我们简单地把 idx 后移一位
        idx += 1
        processed = idx
        in_progress_state["processed"] = processed
        in_progress_state["generated_files"] = [p.name for p in generated]
        save_progress(completed_videos, in_progress_all)

    # 循环结束后判断
    if stop_flag.is_set() or processed < total:
        log(f"⏸️ 暂停在片段 {processed}/{total}，进度已保存")
        return None, in_progress_state

    # ---------- 合成阶段 ----------
    log(f"🔗 合成 {len(generated)} 个片段...")
    if not generated:
        log("❌ 无输出片段可合成")
        if clear_segments:
            clear_directory(segment_dir)
            segment_dir.rmdir()
            log("🗑️ 已删除持久化片段目录")
        return None, None

    def extract_num(fpath):
        m = re.search(r'_(\d+)_', Path(fpath).stem)
        return int(m.group(1)) if m else 0

    generated.sort(key=extract_num)
    concat_txt = SCRIPT_DIR / "concat.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for gf in generated:
            f.write(f"file '{gf.as_posix()}'\n")

    final = FINISH_DIR / f"{video_path.stem}.mp4"
    cmd = [
        ffmpeg_path, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_txt),
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k",
        str(final)
    ]
    r = subprocess.run(cmd, **get_subprocess_kwargs())
    concat_txt.unlink(missing_ok=True)

    # 根据配置清理输出/临时目录
    if clear_output:
        clear_directory(Path(output_dir))
    if clear_temp:
        clear_directory(Path(temp_dir))

    # 根据配置清理片段目录
    if clear_segments:
        clear_directory(segment_dir)
        segment_dir.rmdir()
        log("🗑️ 已删除持久化片段目录")

    if r.returncode != 0:
        log(f"合并失败:\n{r.stderr}")
        return None, None

    log(f"🎉 合成成功: {final.name}")
    return final, None

# ================= 手动合成功能 =================
def manual_merge(video_name, ffmpeg_path):
    safe_folder = FINISH_DIR / video_name
    if not safe_folder.exists() or not safe_folder.is_dir():
        log(f"❌ 目录 {safe_folder} 不存在")
        return False

    clips = list(safe_folder.glob("*.*-audio.mp4"))
    if not clips:
        log(f"❌ 目录 {safe_folder} 中没有找到带 -audio 的片段")
        return False

    def extract_num(fpath):
        m = re.search(r'_(\d+)_', fpath.stem)
        return int(m.group(1)) if m else 0

    clips.sort(key=extract_num)

    concat_txt = SCRIPT_DIR / f"concat_{video_name}.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for cl in clips:
            f.write(f"file '{cl.as_posix()}'\n")

    output_path = FINISH_DIR / f"{video_name}.mp4"
    cmd = [
        ffmpeg_path, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_txt),
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k",
        str(output_path)
    ]
    r = subprocess.run(cmd, **get_subprocess_kwargs())
    concat_txt.unlink(missing_ok=True)

    if r.returncode == 0:
        log(f"🎉 手动合成成功: {output_path.name}")
        original_video = SCRIPT_DIR / f"{video_name}.mp4"
        if original_video.exists():
            try:
                shutil.move(str(original_video), str(DUCE_DIR / original_video.name))
                log(f"📁 原视频已移至 Duce")
            except Exception as e:
                log(f"移动原视频失败: {e}")
        return True
    else:
        log(f"手动合成失败:\n{r.stderr}")
        return False

# ================= GUI 应用 =================
class VideoProcessorApp(Tk):
    def __init__(self):
        super().__init__()
        self.title("MMAudio 批量处理 v3.9.1（竖屏修复）")
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
        """在资源管理器中打开路径所在的文件夹（如果是文件，则打开其父文件夹）"""
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

        # Row 0: ComfyUI 地址
        ttk.Label(config_frame, text="ComfyUI 地址:", style='Header.TLabel').grid(row=0, column=0, sticky=W, padx=5, pady=5)
        self.comfy_url_var = StringVar(value=self.config["comfy_url"])
        ttk.Entry(config_frame, textvariable=self.comfy_url_var, width=40).grid(row=0, column=1, sticky=W, padx=5)

        # Row 1: API 工作流
        ttk.Label(config_frame, text="API 工作流:", style='Header.TLabel').grid(row=1, column=0, sticky=W, padx=5, pady=5)
        self.workflow_path_var = StringVar(value=self.config["workflow_path"])
        ttk.Entry(config_frame, textvariable=self.workflow_path_var, width=50).grid(row=1, column=1, sticky=W, padx=5)
        ttk.Button(config_frame, text="浏览...", command=self.browse_workflow).grid(row=1, column=2, padx=5)
        ttk.Button(config_frame, text="📂", width=3, command=lambda: self._open_folder(self.workflow_path_var)).grid(row=1, column=3, padx=2)

        # Row 2: Output 目录
        ttk.Label(config_frame, text="Output 目录:", style='Header.TLabel').grid(row=2, column=0, sticky=W, padx=5, pady=5)
        self.output_dir_var = StringVar(value=self.config["output_dir"])
        ttk.Entry(config_frame, textvariable=self.output_dir_var, width=50).grid(row=2, column=1, sticky=W, padx=5)
        ttk.Button(config_frame, text="浏览...", command=self.browse_output).grid(row=2, column=2, padx=5)
        ttk.Button(config_frame, text="📂", width=3, command=lambda: self._open_folder(self.output_dir_var)).grid(row=2, column=3, padx=2)

        # Row 3: Temp 目录
        ttk.Label(config_frame, text="Temp 目录:", style='Header.TLabel').grid(row=3, column=0, sticky=W, padx=5, pady=5)
        self.temp_dir_var = StringVar(value=self.config["temp_dir"])
        ttk.Entry(config_frame, textvariable=self.temp_dir_var, width=50).grid(row=3, column=1, sticky=W, padx=5)
        ttk.Button(config_frame, text="浏览...", command=self.browse_temp).grid(row=3, column=2, padx=5)
        ttk.Button(config_frame, text="📂", width=3, command=lambda: self._open_folder(self.temp_dir_var)).grid(row=3, column=3, padx=2)

        # Row 4: FFmpeg 路径
        ttk.Label(config_frame, text="FFmpeg:", style='Header.TLabel').grid(row=4, column=0, sticky=W, padx=5, pady=5)
        self.ffmpeg_path_var = StringVar(value=self.config["ffmpeg_path"])
        ttk.Entry(config_frame, textvariable=self.ffmpeg_path_var, width=40).grid(row=4, column=1, sticky=W, padx=5)
        ttk.Button(config_frame, text="浏览...", command=self.browse_ffmpeg).grid(row=4, column=2, padx=5)
        ttk.Button(config_frame, text="📂", width=3, command=lambda: self._open_folder(self.ffmpeg_path_var)).grid(row=4, column=3, padx=2)

        # Row 5: 合成后清空开关
        ttk.Label(config_frame, text="合成后清空:", style='Header.TLabel').grid(row=5, column=0, sticky=W, padx=5, pady=10)
        switch_frame = ttk.Frame(config_frame)
        switch_frame.grid(row=5, column=1, sticky=W, padx=5)
        ttk.Checkbutton(switch_frame, text="output", variable=self.clear_output_var).pack(side=LEFT, padx=5)
        ttk.Checkbutton(switch_frame, text="temp", variable=self.clear_temp_var).pack(side=LEFT, padx=5)
        ttk.Checkbutton(switch_frame, text="segments", variable=self.clear_segments_var).pack(side=LEFT, padx=5)

        # 控制栏
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

        # 主内容区
        main_frame = ttk.Frame(self)
        main_frame.pack(fill=BOTH, expand=True, padx=10, pady=5)

        # 左侧：待处理视频列表
        list_frame = ttk.LabelFrame(main_frame, text=" 待处理视频 ", padding=5)
        list_frame.pack(side=LEFT, fill=BOTH, expand=True)

        self.listbox = Listbox(list_frame, selectmode="extended", font=("微软雅黑", 9), bg="white")
        self.listbox.pack(side=LEFT, fill=BOTH, expand=True)
        list_scroll = ttk.Scrollbar(list_frame, orient=VERTICAL, command=self.listbox.yview)
        list_scroll.pack(side=RIGHT, fill=Y)
        self.listbox.config(yscrollcommand=list_scroll.set)

        # 中间：未合成视频列表
        incomplete_frame = ttk.LabelFrame(main_frame, text=" 未合成视频 (可手动合成) ", padding=5)
        incomplete_frame.pack(side=LEFT, fill=BOTH, expand=True, padx=(5,0))

        self.incomplete_listbox = Listbox(incomplete_frame, selectmode=SINGLE, font=("微软雅黑", 9), bg="white")
        self.incomplete_listbox.pack(fill=BOTH, expand=True)
        btn_merge = ttk.Button(incomplete_frame, text="🔧 合成选中视频", command=self.manual_merge_selected)
        btn_merge.pack(pady=5)

        # 右侧：实时日志
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