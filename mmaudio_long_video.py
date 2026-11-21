#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MMAudio 长视频自动配音工具
=========================

功能：
  1. 用 ffmpeg 把长视频切成不超过 N 秒的小段（默认 8 秒，适配 12GB 显存）
  2. 裁剪 API 工作流，只保留音频生成链路（去掉 VHS_VideoCombine 视频合成）
  3. 批量提交给本地 ComfyUI，轮询结果并下载生成的音频
  4. 把生成的音频与原视频片段合并
  5. 全部完成后把片段拼接回完整视频（可选音频交叉淡化，避免拼接处突兀）
  6. 自动扫描程序目录下的所有 mp4，逐个排队处理，并保留日志到程序目录

依赖：仅 Python 标准库 + 系统 PATH 中的 ffmpeg / ffprobe
运行：python mmaudio_long_video.py
"""

import copy
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "MMAudio 长视频自动配音工具"
APP_VERSION = "1.1.0"
CONFIG_NAME = "mmaudio_long_video_config.json"
LOG_NAME = "mmaudio_long_video.log"
APP_DIR = Path(__file__).resolve().parent
LOG_PATH = APP_DIR / LOG_NAME
OUTPUT_SUFFIX = "_mmaudio.mp4"


class ToolError(Exception):
    """用户可理解的错误。"""


def resolve_output_path(cfg):
    """输出目录 + 文件名 → 完整路径；兼容旧配置中直接填目录或绝对路径的情况。"""
    src = Path(cfg["input_video"])
    raw = str(cfg.get("output_video", "") or "").strip()
    default_name = src.stem + "_mmaudio.mp4"
    if not raw:
        raw = default_name
    path = Path(raw)
    if path.is_absolute():
        # 旧配置里有人直接把输出目录填进了“输出视频”
        if path.suffix.lower() not in (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v"):
            return path / default_name
        return path
    out_dir = str(cfg.get("output_dir", "") or "").strip()
    if not out_dir:
        out_dir = str(src.parent)
    return Path(out_dir) / path


def scan_mp4_files(folder):
    """扫描程序目录下的待处理 mp4，排除本程序生成的成品。"""
    files = []
    for p in Path(folder).glob("*.mp4"):
        if p.name.lower().endswith(OUTPUT_SUFFIX.lower()):
            continue
        if not p.is_file():
            continue
        files.append(p)
    return sorted(files, key=lambda p: p.name.lower())


def cleanup_comfy_output(mode, own_outputs, comfy_out, app_dir, log):
    """清理 ComfyUI 输出目录。采用移动到程序目录回收站的方式，可恢复。"""
    if mode not in ("own", "all") or not Path(comfy_out).is_dir():
        return 0
    comfy_out = Path(comfy_out)
    trash = Path(app_dir) / ("_comfy_output_trash_" + time.strftime("%Y%m%d_%H%M%S"))
    if mode == "own":
        targets = [comfy_out / str(item.get("subfolder", "")) / item.get("filename", "")
                   for item in own_outputs if item.get("filename")]
    else:
        targets = [p for p in comfy_out.rglob("*") if p.is_file()]
    moved = 0
    for src in targets:
        if not src.is_file():
            continue
        try:
            rel = src.relative_to(comfy_out)
        except ValueError:
            continue
        dst = trash / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved += 1
        except OSError as exc:
            log("移动文件失败 %s：%s" % (src, exc), "warn")
    if mode == "all":
        for d in sorted((x for x in comfy_out.rglob("*") if x.is_dir()),
                        key=lambda x: len(x.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
    log("已清理 ComfyUI 输出：%d 个文件移至 %s" % (moved, trash), "ok")
    return moved


class InterruptedError_(Exception):
    """用户主动停止。"""


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _cmd_list(args):
    return [str(a) for a in args]


def run_command(args, timeout=1800, log=None):
    args = _cmd_list(args)
    if log:
        log("  $ " + " ".join(('"%s"' % a if " " in a else a) for a in args), "cmd")
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except subprocess.TimeoutExpired:
        raise ToolError("命令超时：%s" % os.path.basename(args[0]))
    except FileNotFoundError:
        raise ToolError("找不到可执行文件：%s（请确认已加入 PATH）" % args[0])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        detail = "\n".join(detail.splitlines()[-14:])
        raise ToolError("ffmpeg/ffprobe 执行失败（退出码 %d）：\n%s" % (proc.returncode, detail))
    return proc


def probe_media(path, log=None):
    """返回 duration / fps / width / height / has_audio。"""
    args = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=codec_type,r_frame_rate,avg_frame_rate,width,height",
        "-of", "json", str(path),
    ]
    proc = run_command(args, timeout=120, log=log)
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError("ffprobe 输出解析失败：%s" % exc)
    duration = float(info.get("format", {}).get("duration", 0) or 0)
    video_stream = None
    has_audio = False
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video" and video_stream is None:
            video_stream = stream
        if stream.get("codec_type") == "audio":
            has_audio = True
    if video_stream is None:
        raise ToolError("文件中没有视频流：%s" % path)
    fps = 0.0
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration": duration,
        "fps": fps,
        "width": int(video_stream.get("width", 0) or 0),
        "height": int(video_stream.get("height", 0) or 0),
        "has_audio": has_audio,
    }


def check_ffmpeg():
    for exe in ("ffmpeg", "ffprobe"):
        try:
            run_command([exe, "-version"], timeout=20)
        except ToolError:
            raise ToolError("找不到 %s，请把 ffmpeg 所在目录加入系统 PATH。" % exe)


def make_concat_list(video_paths, list_path):
    lines = []
    for p in video_paths:
        escaped = str(p).replace("\\", "/").replace("'", "'\\''")
        lines.append("file '%s'" % escaped)
    Path(list_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def escape_filter_path(path):
    """ffmpeg filter 中的 Windows 路径转义。"""
    s = str(path).replace("\\", "/")
    s = s.replace(":", "\\:").replace("'", "\\'")
    return s


# ---------------------------------------------------------------------------
# ComfyUI HTTP 客户端
# ---------------------------------------------------------------------------

class ComfyClient:
    def __init__(self, base_url, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client_id = str(uuid.uuid4())

    def _request(self, path, data=None, method=None):
        url = self.base_url + path
        body = None
        headers = {"Accept": "application/json"}
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            msg = raw
            try:
                obj = json.loads(raw)
                msg = obj.get("error", {}).get("message") if isinstance(obj.get("error"), dict) else obj.get("error")
                if not msg and obj.get("node_errors"):
                    parts = []
                    for node_id, err in obj["node_errors"].items():
                        parts.append("节点 %s: %s" % (node_id, err.get("message", err)))
                        for e in err.get("errors", []):
                            parts.append("  - %s" % e.get("message", e))
                    msg = "\n".join(parts)
            except (json.JSONDecodeError, AttributeError):
                pass
            raise ToolError("ComfyUI 返回 HTTP %d：%s" % (exc.code, msg or raw[:600]))
        except urllib.error.URLError as exc:
            raise ToolError(
                "无法连接 ComfyUI（%s）。请确认 ComfyUI 已启动且地址正确。" % exc.reason
            )
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {"_raw": raw.decode("utf-8", "replace")}

    def check_connection(self):
        info = self._request("/system_stats")
        return info

    def object_info(self, node_class=None):
        path = "/object_info" + ("/" + urllib.parse.quote(node_class) if node_class else "")
        return self._request(path)

    def post_prompt(self, workflow):
        return self._request("/prompt", {"prompt": workflow, "client_id": self.client_id})

    def history(self, prompt_id):
        return self._request("/history/" + urllib.parse.quote(prompt_id))

    def interrupt(self):
        try:
            self._request("/interrupt", {}, method="POST")
        except ToolError:
            pass

    def download_view(self, item, dst_path):
        params = urllib.parse.urlencode({
            "filename": item.get("filename", ""),
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        url = self.base_url + "/view?" + params
        req = urllib.request.Request(url, headers={"Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp, open(dst_path, "wb") as fh:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
        except urllib.error.HTTPError as exc:
            raise ToolError("下载生成音频失败 HTTP %d：%s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise ToolError("下载生成音频失败：%s" % exc.reason)


# ---------------------------------------------------------------------------
# 工作流处理
# ---------------------------------------------------------------------------

OUTPUT_NODE_TYPES = {
    "VHS_VideoCombine", "PreviewAudio", "PreviewImage", "PreviewMask",
    "SaveImage", "SaveAudio", "SaveAudioMP3", "SaveAudioOpus", "SaveAudioAdvanced",
    "VHS_VideoCombineAudio", "SaveVideo", "SaveWEBM", "PreviewVideo",
}


def load_workflow(path):
    p = Path(path)
    if not p.is_file():
        raise ToolError("找不到 API 工作流文件：%s" % p)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolError("工作流 JSON 解析失败：%s" % exc)
    if not isinstance(obj, dict) or not obj:
        raise ToolError("工作流 JSON 为空或格式不对，请使用 *_with_api.json 版本。")
    for key, node in obj.items():
        if not isinstance(node, dict) or "class_type" not in node:
            raise ToolError("工作流节点 %s 缺少 class_type，请确认使用的是 API 版本工作流。" % key)
    return obj


def find_nodes(workflow, class_type):
    return [str(k) for k, v in workflow.items() if v.get("class_type") == class_type]


def find_sampler(workflow):
    ids = find_nodes(workflow, "MMAudioSampler")
    if not ids:
        raise ToolError("工作流中未找到 MMAudioSampler 节点，请检查 API 工作流文件。")
    return ids[0]


def find_loader(workflow):
    ids = find_nodes(workflow, "VHS_LoadVideoPath")
    if not ids:
        raise ToolError("工作流中未找到 VHS_LoadVideoPath 节点。")
    return ids[0]


def prune_to_audio(workflow, sampler_id):
    """只保留生成音频所必需的节点，彻底去掉视频合成分支。"""
    keep = set()

    def visit(node_id):
        nid = str(node_id)
        if nid in keep or nid not in workflow:
            return
        keep.add(nid)
        inputs = workflow[nid].get("inputs", {})
        for value in (inputs.values() if isinstance(inputs, dict) else []):
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                ref = value[0]
                if isinstance(ref, str) and ref in workflow:
                    visit(ref)

    visit(sampler_id)
    result = {k: copy.deepcopy(workflow[k]) for k in workflow if k in keep}
    return result


def choose_save_node(client):
    info = client.object_info("SaveAudio")
    if isinstance(info, dict) and "SaveAudio" in info:
        return "SaveAudio", {}
    info = client.object_info("SaveAudioMP3")
    if isinstance(info, dict) and "SaveAudioMP3" in info:
        return "SaveAudioMP3", {"quality": "V0"}
    raise ToolError("ComfyUI 中既没有 SaveAudio 也没有 SaveAudioMP3 节点，无法导出音频。")


def build_audio_prompt(workflow, segment_path, duration, params, save_class, extra_inputs, save_id):
    sampler_id = params["sampler_id"]
    loader_id = params["loader_id"]
    # 先改写输入，再做依赖裁剪，避免把已断开的 VHS_VideoInfo 留在图里
    prompt = copy.deepcopy(workflow)

    if loader_id not in prompt:
        raise ToolError("工作流里没有视频加载节点，请确认 MMAudioSampler 的 images 输入连接正确。")

    prompt[loader_id]["inputs"]["video"] = str(segment_path)
    # 关键：在 VHS 里先把帧缩小再堆叠成张量。
    # MMAudio 内部只会把画面缩到 384x384 / 224x224，送 1080p 是纯浪费，
    # 8 秒 1080p 会生成约 4.6GB 的 float32 张量，导致内存换页和显存碎片。
    prompt[loader_id]["inputs"]["custom_width"] = int(params.get("condition_width", 0) or 0)
    prompt[loader_id]["inputs"]["custom_height"] = int(params.get("condition_height", 0) or 0)
    # 让 MMAudioSampler 使用我们切段的精确时长（原来是 VHS_VideoInfo 的连线）
    prompt[sampler_id]["inputs"]["duration"] = round(float(duration), 3)
    for key in ("steps", "cfg", "seed", "prompt", "negative_prompt",
                "mask_away_clip", "force_offload"):
        if key in params and params[key] is not None:
            prompt[sampler_id]["inputs"][key] = params[key]

    prompt = prune_to_audio(prompt, sampler_id)

    inputs = {"audio": [str(sampler_id), 0], "filename_prefix": params["filename_prefix"]}
    inputs.update(extra_inputs)
    prompt[str(save_id)] = {"inputs": inputs, "class_type": save_class}
    return prompt


def wait_for_result(client, prompt_id, stop_event, log, poll_interval, max_wait):
    started = time.time()
    last_state = ""
    while True:
        if stop_event.is_set():
            client.interrupt()
            raise InterruptedError_("已停止")
        if time.time() - started > max_wait:
            client.interrupt()
            raise ToolError("单段等待超过 %d 秒，已中断该任务。" % max_wait)
        try:
            hist = client.history(prompt_id)
        except ToolError as exc:
            log("轮询历史时出错（将重试）：%s" % exc, "warn")
            time.sleep(poll_interval)
            continue
        entry = hist.get(prompt_id)
        if not entry:
            state = "排队/运行中"
            if state != last_state:
                log("  ComfyUI：%s…" % state, "info")
                last_state = state
            time.sleep(poll_interval)
            continue
        status = entry.get("status", {}) or {}
        status_str = status.get("status_str", "")
        if status_str == "error" or (status.get("messages") and any(
                isinstance(m, list) and m and m[0] == "execution_error" for m in status["messages"])):
            errs = []
            for msg in status.get("messages", []):
                if isinstance(msg, list) and msg and msg[0] == "execution_error" and len(msg) > 1:
                    errs.append(str(msg[1].get("exception_message", msg[1])))
            raise ToolError("ComfyUI 执行失败：%s" % ("\n".join(errs) or "未知错误"))
        if status.get("completed"):
            return entry.get("outputs", {}) or {}
        state = "执行中"
        if state != last_state:
            log("  ComfyUI：%s…" % state, "info")
            last_state = state
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------

class Pipeline:
    def __init__(self, cfg, callbacks):
        self.cfg = cfg
        self.log = callbacks["log"]
        self.progress = callbacks["progress"]
        self.stop_event = callbacks["stop"]
        self.done = callbacks["done"]
        self.generated_outputs = []

    def _check_stop(self):
        if self.stop_event.is_set():
            raise InterruptedError_("已停止")

    def run(self):
        work_dir = None
        success = False
        try:
            check_ffmpeg()
            cfg = self.cfg
            src = Path(cfg["input_video"])
            out = resolve_output_path(cfg)
            workflow_path = Path(cfg["workflow_json"])
            if not src.is_file():
                raise ToolError("输入视频不存在：%s" % src)
            if not workflow_path.is_file():
                raise ToolError("API 工作流不存在：%s" % workflow_path)
            out.parent.mkdir(parents=True, exist_ok=True)

            media = probe_media(src, self.log)
            self.log("源视频：时长 %.2fs，帧率 %.3f，%dx%d，%s" % (
                media["duration"], media["fps"], media["width"], media["height"],
                "有音轨" if media["has_audio"] else "无音轨"), "ok")
            if media["duration"] <= 0:
                raise ToolError("无法读取视频时长。")

            workflow = load_workflow(workflow_path)
            sampler_id = find_sampler(workflow)
            loader_id = find_loader(workflow)
            self.log("工作流节点：MMAudioSampler=%s, LoadVideoPath=%s" % (sampler_id, loader_id), "info")

            client = ComfyClient(cfg["comfy_url"], timeout=int(cfg["request_timeout"]))
            try:
                client.check_connection()
            except ToolError as exc:
                raise ToolError("%s" % exc)
            self.log("ComfyUI 连接正常：%s" % cfg["comfy_url"], "ok")
            save_class, extra_inputs = choose_save_node(client)
            self.log("音频导出节点：%s" % save_class, "info")

            # 工作目录：与输出文件同级的隐藏临时目录
            work_dir = out.parent / ("_mmaudio_work_" + src.stem)
            work_dir.mkdir(parents=True, exist_ok=True)
            seg_dir = work_dir / "segments"
            aud_dir = work_dir / "audio"
            seg_dir.mkdir(exist_ok=True)
            aud_dir.mkdir(exist_ok=True)

            segments = self._split(src, media, seg_dir)
            self.log("共切出 %d 段（每段 ≤ %.2fs）" % (len(segments), float(cfg["max_duration"])), "ok")

            audio_files = []
            total = len(segments)
            for idx, seg in enumerate(segments):
                self._check_stop()
                self.progress(idx, total, "生成音频 第 %d/%d 段" % (idx + 1, total))
                audio_files.append(self._generate_audio(client, workflow, sampler_id, loader_id,
                                                        save_class, extra_inputs, seg, idx, aud_dir))

            merged_videos = []
            for idx, seg in enumerate(segments):
                self._check_stop()
                self.progress(idx, total, "合并音视频 第 %d/%d 段" % (idx + 1, total))
                merged = seg_dir / ("merged_%04d.mp4" % idx)
                self._merge(seg["path"], audio_files[idx], merged, seg)
                merged_videos.append(merged)

            self.progress(total, total, "拼接完整视频")
            self._finalize(merged_videos, audio_files, segments, out)
            success = True
            self.log("完成！输出：%s" % out, "ok")
            self.done(True, str(out))
        except InterruptedError_ as exc:
            self.log(str(exc), "warn")
            self.done(False, "已停止")
        except ToolError as exc:
            self.log("错误：%s" % exc, "error")
            self.done(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            import traceback
            self.log("未预期的错误：%s\n%s" % (exc, traceback.format_exc()), "error")
            self.done(False, str(exc))
        finally:
            if work_dir and success and not self.cfg.get("keep_temp", False):
                shutil.rmtree(work_dir, ignore_errors=True)
                self.log("已清理中间文件。", "info")
            elif work_dir and not success:
                self.log("保留了中间文件便于排查：%s" % work_dir, "info")

    # ---- 分段 ----
    def _split(self, src, media, seg_dir):
        cfg = self.cfg
        step = float(cfg["max_duration"])
        if step <= 0 or step > 60:
            raise ToolError("分段时长应在 0~60 秒之间。")
        fps = float(cfg["condition_fps"])
        if fps <= 0:
            raise ToolError("条件帧率必须大于 0。")
        total = media["duration"]
        segments = []
        idx = 0
        start = 0.0
        while start < total - 0.001:
            self._check_stop()
            dur = min(step, total - start)
            path = seg_dir / ("clip_%04d.mp4" % idx)
            if path.exists() and path.stat().st_size > 1024:
                self.log("  [跳过] 分段 %d 已存在" % idx, "info")
            else:
                self.log("  切分第 %d 段：%.3f ~ %.3f s" % (idx + 1, start, start + dur), "info")
                args = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", "%.6f" % start, "-i", src, "-t", "%.6f" % dur,
                    "-an", "-vf", "fps=%.6f,scale=trunc(iw/2)*2:trunc(ih/2)*2" % fps,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", path,
                ]
                run_command(args, timeout=1800, log=None)
            real_dur = probe_media(path).get("duration", dur)
            segments.append({"index": idx, "path": path, "start": start,
                             "duration": real_dur})
            idx += 1
            start += dur
        if not segments:
            raise ToolError("视频太短，未能切出任何片段。")
        return segments

    # ---- 提交 ComfyUI 并下载音频 ----
    def _generate_audio(self, client, workflow, sampler_id, loader_id,
                        save_class, extra_inputs, seg, idx, aud_dir):
        self._check_stop()
        out_path = aud_dir / ("audio_%04d.flac" % idx)
        if out_path.exists() and out_path.stat().st_size > 1024:
            self.log("  第 %d 段音频已存在，跳过生成" % (idx + 1), "info")
            return out_path

        seed_mode = self.cfg.get("seed_mode", "fixed")
        base_seed = int(self.cfg.get("seed", 0))
        if seed_mode == "increment":
            seed = base_seed + idx
        elif seed_mode == "random":
            seed = random.randint(0, 0xFFFFFFFFFFFFFFFF)
        else:
            seed = base_seed

        params = {
            "sampler_id": sampler_id,
            "loader_id": loader_id,
            "condition_width": int(self.cfg.get("condition_width", 0) or 0),
            "condition_height": int(self.cfg.get("condition_height", 0) or 0),
            "filename_prefix": "mmaudio_batch/seg_%04d" % idx,
            "steps": int(self.cfg["steps"]),
            "cfg": float(self.cfg["cfg"]),
            "seed": seed,
            "prompt": self.cfg["prompt"],
            "negative_prompt": self.cfg["negative_prompt"],
            "mask_away_clip": bool(self.cfg.get("mask_away_clip", False)),
            "force_offload": bool(self.cfg.get("force_offload", True)),
        }
        prompt = build_audio_prompt(workflow, seg["path"], seg["duration"], params,
                                    save_class, extra_inputs, save_id=9000)

        retries = max(0, int(self.cfg.get("retries", 2)))
        last_err = None
        for attempt in range(retries + 1):
            self._check_stop()
            try:
                self.log("  第 %d 段：提交 ComfyUI（时长 %.3fs，seed=%d）%s" % (
                    idx + 1, seg["duration"], seed,
                    "，重试 %d/%d" % (attempt, retries) if attempt else ""), "info")
                resp = client.post_prompt(prompt)
                prompt_id = resp.get("prompt_id")
                if not prompt_id:
                    raise ToolError("ComfyUI 未返回 prompt_id：%s" % resp)
                outputs = wait_for_result(client, prompt_id, self.stop_event, self.log,
                                          float(self.cfg["poll_interval"]),
                                          int(self.cfg["max_wait"]))
                node_out = outputs.get("9000")
                if not node_out:
                    for key, value in outputs.items():
                        if isinstance(value, dict) and value.get("audio"):
                            node_out = value
                            break
                if not node_out or not node_out.get("audio"):
                    raise ToolError("ComfyUI 输出中没有音频，检查 SaveAudio 节点是否执行。")
                client.download_view(node_out["audio"][0], out_path)
                if node_out["audio"][0] not in self.generated_outputs:
                    self.generated_outputs.append(dict(node_out["audio"][0]))
                if out_path.stat().st_size < 1024:
                    raise ToolError("下载的音频文件为空。")
                self.log("  第 %d 段音频生成完成：%s" % (idx + 1, out_path.name), "ok")
                return out_path
            except InterruptedError_:
                raise
            except ToolError as exc:
                last_err = exc
                self.log("  第 %d 段失败：%s" % (idx + 1, exc), "error")
                if attempt < retries:
                    time.sleep(1.5)
        raise ToolError("第 %d 段多次重试后仍失败：%s" % (idx + 1, last_err))

    # ---- 音频与原视频合并 ----
    def _merge(self, video, audio, out_path, seg):
        self._check_stop()
        dur = seg["duration"]
        mode = self.cfg.get("audio_mode", "replace")
        if out_path.exists() and out_path.stat().st_size > 1024:
            self.log("  [跳过] 合并结果已存在：%s" % out_path.name, "info")
            return
        if mode == "mix":
            vol = float(self.cfg.get("original_volume", 0.3))
            afilter = (
                "[1:a]atrim=0:{d},asetpts=PTS-STARTPTS,volume={v}[oa];"
                "[2:a]apad,atrim=0:{d},asetpts=PTS-STARTPTS[ga];"
                "[oa][ga]amix=inputs=2:duration=first:dropout_transition=0[a]"
            ).format(d="%.6f" % dur, v="%.4f" % vol)
            args = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", video, "-ss", "%.6f" % seg["start"], "-t", "%.6f" % dur,
                "-i", self.cfg["input_video"], "-i", audio,
                "-filter_complex", afilter,
                "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-t", "%.6f" % dur,
                "-movflags", "+faststart", out_path,
            ]
        else:
            afilter = "[1:a]apad,atrim=0:{d},asetpts=PTS-STARTPTS[a]".format(d="%.6f" % dur)
            args = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", video, "-i", audio,
                "-filter_complex", afilter,
                "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-t", "%.6f" % dur,
                "-movflags", "+faststart", out_path,
            ]
        run_command(args, timeout=1800, log=None)

    # ---- 最终拼接 ----
    def _finalize(self, merged_videos, audio_files, segments, out_path):
        self._check_stop()
        work_dir = out_path.parent / ("_mmaudio_work_" + Path(self.cfg["input_video"]).stem)
        concat_list = work_dir / "concat_list.txt"
        make_concat_list(merged_videos, concat_list)
        self.log("  拼接 %d 个片段…" % len(merged_videos), "info")
        args = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c", "copy", "-movflags", "+faststart", out_path,
        ]
        try:
            run_command(args, timeout=3600, log=None)
        except ToolError as exc:
            self.log("  无损拼接失败，改用重编码拼接：%s" % exc, "warn")
            args = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", concat_list,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", out_path,
            ]
            run_command(args, timeout=7200, log=None)

        crossfade = float(self.cfg.get("crossfade", 0.0) or 0.0)
        if crossfade > 0 and len(audio_files) > 1:
            self.log("  应用 %.3fs 音频交叉淡化…" % crossfade, "info")
            try:
                self._apply_crossfade(merged_videos, audio_files, segments, out_path, work_dir, crossfade)
            except ToolError as exc:
                self.log("  交叉淡化失败，保留普通拼接结果：%s" % exc, "warn")

    def _apply_crossfade(self, merged_videos, audio_files, segments, out_path, work_dir, crossfade):
        total_dur = probe_media(out_path)["duration"]
        norm_inputs = []
        for i, (audio_file, seg) in enumerate(zip(audio_files, segments)):
            dst = work_dir / ("xfade_%04d.wav" % i)
            # 每段尾部补 crossfade 秒，保证交叉后总时长不缩短
            pad_dur = seg["duration"] + (crossfade if i < len(audio_files) - 1 else 0)
            args = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", audio_file,
                "-af", "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=0:%.6f" % pad_dur,
                "-c:a", "pcm_s16le", dst,
            ]
            run_command(args, timeout=600, log=None)
            norm_inputs.append(dst)

        lines = []
        for i in range(len(norm_inputs)):
            lines.append("[%d:a]asetpts=PTS-STARTPTS[a%d];" % (i, i))
        prev = "a0"
        for i in range(1, len(norm_inputs)):
            out_label = "x%d" % i
            lines.append("[%s][a%d]acrossfade=d=%.6f:c1=tri:c2=tri[%s];" % (
                prev, i, crossfade, out_label))
            prev = out_label
        lines.append("[%s]atrim=0:%.6f,asetpts=PTS-STARTPTS[afinal]" % (prev, total_dur))
        filter_script = "".join(lines)
        if len(filter_script) > 24000:
            raise ToolError("片段过多，交叉淡化滤镜超过命令行长度限制；请调小交叉淡化或关闭该功能。")

        tmp_out = work_dir / "final_xfade.mp4"
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", work_dir / "concat_list.txt"]
        for p in norm_inputs:
            args += ["-i", p]
        args += [
            "-filter_complex", filter_script,
            "-map", "0:v:0", "-map", "[afinal]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", tmp_out,
        ]
        run_command(args, timeout=7200, log=None)
        os.replace(tmp_out, out_path)


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------

BG = "#1e1e1e"
PANEL = "#252526"
PANEL2 = "#2d2d30"
FG = "#d4d4d4"
MUTED = "#9d9d9d"
ACCENT = "#0e639c"
OK = "#4ec9b0"
WARN = "#dcdcaa"
ERR = "#f48771"

DEFAULT_CONFIG = {
    "input_video": "",
    "output_dir": str(APP_DIR),
    "output_video": "",
    "workflow_json": str(APP_DIR / "mmaudio_NSFW_bath_to_open _with_api.json"),
    "comfy_url": "http://127.0.0.1:8188",
    "prompt": "anime girl voice",
    "negative_prompt": "talking, speech, dialogue, music, singing, male voice, background noise, dry sounds, calm, whisper, ",
    "steps": 25,
    "cfg": 4.5,
    "seed": 184852840990222,
    "seed_mode": "fixed",
    "mask_away_clip": False,
    "force_offload": True,
    "max_duration": 8.0,
    "condition_fps": 25.0,
    "condition_width": 384,
    "condition_height": 216,
    "audio_mode": "replace",
    "original_volume": 0.3,
    "crossfade": 0.15,
    "retries": 2,
    "keep_temp": True,
    "poll_interval": 0.8,
    "max_wait": 3600,
    "request_timeout": 30,
    "comfy_output_dir": r"E:\Ordrnary\ComfyUI-aki-v3\ComfyUI\output",
    "clean_output": "none",
    "temp_policy_migrated": False,
}



class App:
    def __init__(self, root):
        self.root = root
        self.root.title("%s  v%s" % (APP_TITLE, APP_VERSION))
        self.root.geometry("1560x920")
        self.root.minsize(1180, 760)
        self.root.configure(bg=BG)
        self.cfg_path = APP_DIR / CONFIG_NAME
        self.cfg = self.load_config()
        self.ui_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.pending_files = {}
        self.completed_files = {}
        self.duration_cache = {}
        self._build_style()
        self._build_ui()
        self._load_fields()
        self.refresh_video_list()
        self.log("程序启动：%s" % APP_DIR, "info")
        self.log("日志同时写入：%s" % LOG_PATH, "info")
        self.root.after(120, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- 配置 ----
    def load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        if self.cfg_path.is_file():
            try:
                cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        # 所有产物和临时文件都固定在程序目录
        cfg["output_dir"] = str(APP_DIR)
        # 工作流找不到时回退到程序目录里的副本
        wf = Path(str(cfg.get("workflow_json", "") or ""))
        if not wf.is_file():
            cfg["workflow_json"] = str(APP_DIR / "mmaudio_NSFW_bath_to_open _with_api.json")
        # 一次性迁移：新版本默认保留中间文件
        if not cfg.get("temp_policy_migrated"):
            cfg["keep_temp"] = True
            cfg["temp_policy_migrated"] = True
        return cfg

    def save_config(self):
        self._collect_fields()
        try:
            self.cfg_path.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---- 样式 ----
    def _build_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TLabelframe", background=PANEL, foreground=FG, bordercolor="#3e3e42")
        style.configure("Card.TLabelframe.Label", background=PANEL, foreground="#569cd6",
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TLabel", background=PANEL, foreground=FG, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("TEntry", fieldbackground=PANEL2, foreground=FG, insertcolor=FG, bordercolor="#3e3e42")
        style.configure("TButton", background=PANEL2, foreground=FG, bordercolor="#3e3e42", padding=6,
                        font=("Microsoft YaHei UI", 10))
        style.map("TButton", background=[("active", "#3e3e42")])
        style.configure("Primary.TButton", background=ACCENT, foreground="#ffffff", padding=8,
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#1479bb")])
        style.configure("Stop.TButton", background="#8b2e2e", foreground="#ffffff", padding=8,
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TCheckbutton", background=PANEL, foreground=FG, font=("Microsoft YaHei UI", 10))
        style.configure("TRadiobutton", background=PANEL, foreground=FG, font=("Microsoft YaHei UI", 10))
        style.configure("TProgressbar", background=ACCENT, troughcolor=PANEL2, bordercolor=PANEL2)
        style.configure("TCombobox", fieldbackground=PANEL2, foreground=FG, background=PANEL2,
                        arrowcolor=FG, bordercolor="#3e3e42")
        style.configure("Treeview", background=PANEL2, fieldbackground=PANEL2, foreground=FG,
                        bordercolor="#3e3e42", rowheight=24, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", background=PANEL, foreground="#569cd6",
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", ACCENT)])

    # ---- UI ----
    def _build_ui(self):
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)
        hpane = ttk.PanedWindow(main, orient="horizontal")
        hpane.pack(fill="both", expand=True)

        # 左侧：上部设置 + 下部两个列表
        left = ttk.Frame(hpane)
        hpane.add(left, weight=3)
        head = ttk.Frame(left)
        head.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(head, text=APP_TITLE, bg=BG, fg="#569cd6",
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        tk.Label(head, text="自动扫描程序目录 · 批量处理 · 日志留档",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=12, pady=(6, 0))

        vpane = ttk.PanedWindow(left, orient="vertical")
        vpane.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        top = ttk.Frame(vpane)
        bottom = ttk.Frame(vpane)
        vpane.add(top, weight=3)
        vpane.add(bottom, weight=2)

        # 操作条固定在设置区底部
        action = ttk.Frame(top)
        action.pack(side="bottom", fill="x", padx=6, pady=(2, 6))
        self.btn_start = ttk.Button(action, text="开始处理全部", style="Primary.TButton",
                                    command=self._start, width=17)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(action, text="停止", style="Stop.TButton",
                                   command=self._stop, width=9, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(action, text="刷新列表", command=self.refresh_video_list, width=11).pack(side="left")
        ttk.Button(action, text="打开程序目录", command=self._open_folder, width=14).pack(side="left", padx=6)
        self.status = ttk.Label(action, text="就绪", foreground=MUTED, width=42, anchor="w")
        self.status.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(action, mode="determinate", length=160)
        self.progress.pack(side="left", fill="x", expand=True)

        canvas = tk.Canvas(top, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(top, orient="vertical", command=canvas.yview)
        self.settings = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=self.settings, anchor="nw")
        self.settings.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        self.vars = {}
        # 路径与连接
        card = ttk.LabelFrame(self.settings, text=" 路径与连接 ", style="Card.TLabelframe")
        card.pack(fill="x", padx=8, pady=(8, 6))
        self.vars["workflow_json"] = tk.StringVar()
        ttk.Label(card, text="API 工作流 JSON").grid(row=0, column=0, sticky="w", padx=10, pady=5)
        ttk.Entry(card, textvariable=self.vars["workflow_json"], width=78).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Button(card, text="浏览…", command=lambda: self._browse("workflow_json", "json"), width=9).grid(
            row=0, column=2, padx=(6, 10), pady=5)
        self.vars["comfy_url"] = tk.StringVar()
        ttk.Label(card, text="ComfyUI 地址").grid(row=1, column=0, sticky="w", padx=10, pady=5)
        ttk.Entry(card, textvariable=self.vars["comfy_url"], width=40).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Button(card, text="预检连接", command=self._precheck, width=9).grid(
            row=1, column=2, padx=(6, 10), pady=5)
        self.vars["comfy_output_dir"] = tk.StringVar()
        ttk.Label(card, text="ComfyUI output").grid(row=2, column=0, sticky="w", padx=10, pady=5)
        ttk.Entry(card, textvariable=self.vars["comfy_output_dir"], width=78).grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Button(card, text="浏览…", command=lambda: self._browse("comfy_output_dir", "dir"), width=9).grid(
            row=2, column=2, padx=(6, 10), pady=5)
        self.vars["clean_output"] = tk.StringVar()
        ttk.Label(card, text="每个视频完成后清理").grid(row=3, column=0, sticky="w", padx=10, pady=5)
        ttk.Combobox(card, textvariable=self.vars["clean_output"], state="readonly", width=32,
                     values=["none", "own", "all"]).grid(row=3, column=1, sticky="w", pady=5)
        tk.Label(card, text="none=不清理 / own=仅移走本程序生成 / all=移走 output 内全部文件（会二次确认）",
                 bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).grid(
            row=4, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))
        card.columnconfigure(1, weight=1)

        # 生成参数
        card2 = ttk.LabelFrame(self.settings, text=" MMAudio 生成参数 ", style="Card.TLabelframe")
        card2.pack(fill="x", padx=8, pady=6)
        ttk.Label(card2, text="提示词 Prompt").grid(row=0, column=0, sticky="nw", padx=10, pady=5)
        self.txt_prompt = tk.Text(card2, height=3, bg=PANEL2, fg=FG, insertbackground=FG,
                                  relief="flat", font=("Microsoft YaHei UI", 10), wrap="word")
        self.txt_prompt.grid(row=0, column=1, columnspan=3, sticky="ew", pady=5)
        ttk.Label(card2, text="负向提示词").grid(row=1, column=0, sticky="nw", padx=10, pady=5)
        self.txt_neg = tk.Text(card2, height=3, bg=PANEL2, fg=FG, insertbackground=FG,
                               relief="flat", font=("Microsoft YaHei UI", 10), wrap="word")
        self.txt_neg.grid(row=1, column=1, columnspan=3, sticky="ew", pady=5)
        self.vars["steps"] = tk.StringVar()
        self.vars["cfg"] = tk.StringVar()
        self.vars["seed"] = tk.StringVar()
        for col, (key, label, width) in enumerate((
            ("steps", "Steps", 10), ("cfg", "CFG", 10), ("seed", "Seed", 24),
        )):
            ttk.Label(card2, text=label).grid(row=2, column=col * 2, sticky="w", padx=(10, 4), pady=6)
            ttk.Entry(card2, textvariable=self.vars[key], width=width).grid(
                row=2, column=col * 2 + 1, sticky="w", pady=6)
        self.vars["seed_mode"] = tk.StringVar()
        ttk.Label(card2, text="种子模式").grid(row=3, column=0, sticky="w", padx=10, pady=6)
        ttk.Combobox(card2, textvariable=self.vars["seed_mode"], state="readonly", width=22,
                     values=["fixed", "increment", "random"]).grid(row=3, column=1, sticky="w", pady=6)
        self.vars["force_offload"] = tk.BooleanVar()
        self.vars["mask_away_clip"] = tk.BooleanVar()
        ttk.Checkbutton(card2, text="force_offload（节省显存）",
                        variable=self.vars["force_offload"]).grid(row=3, column=2, sticky="w", pady=6)
        ttk.Checkbutton(card2, text="mask_away_clip",
                        variable=self.vars["mask_away_clip"]).grid(row=3, column=3, sticky="w", pady=6)
        card2.columnconfigure(1, weight=1)

        # 分段与合成
        card3 = ttk.LabelFrame(self.settings, text=" 分段与合成 ", style="Card.TLabelframe")
        card3.pack(fill="x", padx=8, pady=(6, 10))
        for key in ("max_duration", "condition_fps", "condition_width", "condition_height",
                    "original_volume", "crossfade", "retries"):
            self.vars[key] = tk.StringVar()
        fields = (
            ("max_duration", "最大分段时长(s)", 12),
            ("condition_fps", "条件帧率(≥25)", 12),
            ("condition_width", "送入 MMAudio 宽度", 12),
            ("condition_height", "送入 MMAudio 高度", 12),
            ("original_volume", "原声音量(混合模式)", 14),
            ("crossfade", "音频交叉淡化(s)", 14),
            ("retries", "失败重试次数", 12),
        )
        for i, (key, label, width) in enumerate(fields):
            col, r = (i % 3) * 2, (i // 3) * 2
            ttk.Label(card3, text=label).grid(row=r, column=col, sticky="w", padx=(10, 4), pady=5)
            ttk.Entry(card3, textvariable=self.vars[key], width=width).grid(
                row=r, column=col + 1, sticky="w", pady=5)
        self.vars["audio_mode"] = tk.StringVar()
        self.vars["keep_temp"] = tk.BooleanVar()
        ttk.Label(card3, text="音频模式").grid(row=3, column=0, sticky="w", padx=10, pady=5)
        ttk.Combobox(card3, textvariable=self.vars["audio_mode"], state="readonly", width=22,
                     values=["replace", "mix"]).grid(row=3, column=1, sticky="w", pady=5)
        ttk.Checkbutton(card3, text="保留中间文件（程序目录内）",
                        variable=self.vars["keep_temp"]).grid(row=3, column=2, sticky="w", pady=5)
        tk.Label(card3, text="宽高建议 384×216（8 秒 1080p 送原分辨率会占约 4.6GB 并严重降速）。\n"
                             "输出文件与各视频的 _mmaudio_work_ 临时目录均在本程序文件夹。",
                 bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9), justify="left").grid(
            row=4, column=0, columnspan=6, sticky="w", padx=10, pady=(2, 8))

        # 下部：待处理 / 已完成
        lists = ttk.Frame(bottom)
        lists.pack(fill="both", expand=True, padx=6, pady=(4, 4))
        pending_lf = ttk.LabelFrame(lists, text=" 待处理视频 ", style="Card.TLabelframe")
        pending_lf.pack(side="left", fill="both", expand=True, padx=(4, 4))
        done_lf = ttk.LabelFrame(lists, text=" 已完成视频 ", style="Card.TLabelframe")
        done_lf.pack(side="right", fill="both", expand=True, padx=(4, 4))
        self.pending_tree = self._make_tree(pending_lf)
        self.completed_tree = self._make_tree(done_lf)
        self.completed_tree.tag_configure("ok", foreground=OK)
        self.completed_tree.tag_configure("err", foreground=ERR)

        # 右侧日志
        log_box = ttk.LabelFrame(hpane, text=" 运行日志（同时写入 mmaudio_long_video.log） ",
                                 style="Card.TLabelframe")
        hpane.add(log_box, weight=2)
        self.log_text = tk.Text(log_box, bg="#1a1a1a", fg=FG, insertbackground=FG,
                                relief="flat", font=("Consolas", 9), wrap="none")
        self.log_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        for level, color in (("info", FG), ("cmd", MUTED), ("ok", OK), ("warn", WARN), ("error", ERR)):
            self.log_text.tag_configure(level, foreground=color)
        log_scroll = ttk.Scrollbar(log_box, command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y", pady=6)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        ttk.Button(log_box, text="打开日志", command=self._open_log, width=11).place(
            relx=1.0, rely=0.0, x=-100, y=4)

    def _make_tree(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        tree = ttk.Treeview(frame, columns=("name", "size", "duration"), show="headings", height=8)
        tree.heading("name", text="文件名")
        tree.heading("size", text="大小")
        tree.heading("duration", text="时长")
        tree.column("name", width=280, anchor="w")
        tree.column("size", width=90, anchor="e")
        tree.column("duration", width=70, anchor="e")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        sb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=sb.set)
        return tree

    # ---- 列表 ----
    def refresh_video_list(self):
        files = scan_mp4_files(APP_DIR)
        self.pending_files = {p.name: p for p in files if p.name not in self.completed_files}
        for item in self.pending_tree.get_children():
            self.pending_tree.delete(item)
        for name, path in self.pending_files.items():
            self.pending_tree.insert("", "end", values=(name, self._size_text(path), self._duration_text(path)))
        self._update_counts()

    def _update_counts(self):
        self.status.configure(text="待处理 %d 个 / 已完成 %d 个" % (
            len(self.pending_files), len(self.completed_files)))

    @staticmethod
    def _size_text(path):
        try:
            n = path.stat().st_size
        except OSError:
            return "-"
        return "%.1f MB" % (n / 1024 / 1024) if n < 1024 ** 3 else "%.2f GB" % (n / 1024 ** 3)

    def _duration_text(self, path):
        key = str(path)
        if key not in self.duration_cache:
            try:
                self.duration_cache[key] = "%.0fs" % probe_media(path)["duration"]
            except (ToolError, OSError):
                self.duration_cache[key] = "-"
        return self.duration_cache[key]

    # ---- 配置 / 字段 ----
    def _load_fields(self):
        for key, var in self.vars.items():
            if isinstance(var, tk.BooleanVar):
                var.set(bool(self.cfg.get(key, False)))
            else:
                var.set(str(self.cfg.get(key, "")))
        self.txt_prompt.insert("1.0", self.cfg.get("prompt", ""))
        self.txt_neg.insert("1.0", self.cfg.get("negative_prompt", ""))

    def _collect_fields(self):
        for key, var in self.vars.items():
            self.cfg[key] = bool(var.get()) if isinstance(var, tk.BooleanVar) else var.get().strip()
        self.cfg["prompt"] = self.txt_prompt.get("1.0", "end").strip()
        self.cfg["negative_prompt"] = self.txt_neg.get("1.0", "end").strip()

    def _browse(self, key, kind):
        if kind == "json":
            path = filedialog.askopenfilename(title="选择 API 工作流", filetypes=[
                ("JSON", "*.json"), ("所有文件", "*.*")])
        else:
            path = filedialog.askdirectory(title="选择目录")
        if path:
            self.vars[key].set(path)

    def _open_folder(self):
        try:
            os.startfile(str(APP_DIR))  # noqa: S606
        except OSError:
            self.log("无法打开目录：%s" % APP_DIR, "warn")

    def _open_log(self):
        try:
            if LOG_PATH.exists():
                os.startfile(str(LOG_PATH))  # noqa: S606
            else:
                self.log("日志文件尚未生成。", "warn")
        except OSError:
            self.log("无法打开日志：%s" % LOG_PATH, "warn")

    # ---- 日志 ----
    def log(self, message, level="info"):
        self.ui_queue.put(("log", message, level))

    def _write_log(self, ts, msg, level="info"):
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
                backup = LOG_PATH.with_name(LOG_NAME + ".old")
                if backup.exists():
                    backup.unlink()
                LOG_PATH.rename(backup)
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write("[%s][%s] %s\n" % (ts, level.upper(), msg))
        except OSError:
            pass

    def _drain_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    _, msg, level = item
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.log_text.insert("end", "[%s] %s\n" % (ts, msg), level)
                    self.log_text.see("end")
                    self._write_log(ts, msg, level)
                elif kind == "batch_progress":
                    _, cur, total, text = item
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = cur
                    self.status.configure(text=text, foreground=ACCENT)
                elif kind == "segment_progress":
                    _, cur, total, text = item
                    self.status.configure(text="%s (%d/%d)" % (text, cur, total), foreground=ACCENT)
                elif kind == "video_start":
                    _, name = item
                    self.log("=" * 70, "info")
                    self.log("开始处理：%s" % name, "info")
                elif kind == "video_done":
                    _, name, ok, msg = item
                    for iid in self.pending_tree.get_children():
                        if self.pending_tree.item(iid, "values")[0] == name:
                            values = self.pending_tree.item(iid, "values")
                            self.pending_tree.delete(iid)
                            self.completed_files[name] = self.pending_files.pop(name, None)
                            self.completed_tree.insert("", "end", values=(values[0], values[1], msg[:60]),
                                                       tags=("ok" if ok else "err",))
                            break
                    self._update_counts()
                    if not ok:
                        self.status.configure(text="失败：%s" % name, foreground=ERR)
                elif kind == "batch_done":
                    _, total = item
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.status.configure(text="全部结束（共 %d 个）" % total, foreground=OK)
                    self.progress["value"] = self.progress["maximum"]
                    messagebox.showinfo("批量处理结束", "本次共处理 %d 个视频，详情见日志。" % total)
        except queue.Empty:
            pass
        self.root.after(120, self._drain_queue)

    # ---- 操作 ----
    def _precheck(self):
        self._collect_fields()
        self.save_config()
        problems = []
        if not self.cfg["workflow_json"] or not Path(self.cfg["workflow_json"]).is_file():
            problems.append("API 工作流文件不存在")
        try:
            check_ffmpeg()
        except ToolError as exc:
            problems.append(str(exc))
        try:
            client = ComfyClient(self.cfg["comfy_url"], timeout=10)
            client.check_connection()
            choose_save_node(client)
        except ToolError as exc:
            problems.append(str(exc))
        if not Path(self.cfg["comfy_output_dir"]).is_dir():
            problems.append("ComfyUI output 目录不存在（清理选项会不可用）")
        if problems:
            self.log("预检未通过：\n  - " + "\n  - ".join(problems), "error")
            messagebox.showerror("预检未通过", "\n".join(problems))
        else:
            self.log("预检通过：ffmpeg / ComfyUI / 工作流 / output 目录均可用。", "ok")
            messagebox.showinfo("预检通过", "环境就绪。")

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        self._collect_fields()
        self.save_config()
        self.refresh_video_list()
        problems = []
        if not self.pending_files:
            problems.append("程序目录下没有待处理的 mp4")
        if not self.cfg["workflow_json"] or not Path(self.cfg["workflow_json"]).is_file():
            problems.append("请选择 API 工作流 JSON")
        try:
            float(self.cfg["max_duration"])
            float(self.cfg["condition_fps"])
            int(self.cfg.get("condition_width", 0) or 0)
            int(self.cfg.get("condition_height", 0) or 0)
            int(self.cfg["steps"])
            float(self.cfg["cfg"])
            int(self.cfg["seed"])
        except ValueError:
            problems.append("参数中有非法数字，请检查 Steps / CFG / Seed / 分段时长 / 宽高")
        if self.cfg.get("clean_output") == "all":
            out_dir = Path(self.cfg["comfy_output_dir"])
            count = len(list(out_dir.glob("**/*"))) if out_dir.is_dir() else 0
            if not messagebox.askyesno(
                    "危险操作确认",
                    "你选择了清理 ComfyUI output 内的全部文件。\n\n"
                    "目录：%s\n涉及条目：约 %d 个\n\n"
                    "文件会被移动到程序目录的 _comfy_output_trash_ 时间戳文件夹（可恢复），"
                    "不是永久删除。确定继续吗？" % (out_dir, count)):
                return
        if problems:
            messagebox.showerror("参数检查失败", "\n".join(problems))
            return

        videos = [self.pending_files[n] for n in list(self.pending_files)]
        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress["value"] = 0
        self.progress["maximum"] = max(len(videos), 1)

        def worker():
            total = len(videos)
            for i, video in enumerate(videos, 1):
                if self.stop_event.is_set():
                    break
                self.ui_queue.put(("video_start", video.name))
                self.ui_queue.put(("batch_progress", i, total,
                                   "第 %d/%d 个：%s" % (i, total, video.name)))
                cfg_v = dict(self.cfg)
                cfg_v["input_video"] = str(video)
                cfg_v["output_dir"] = str(APP_DIR)
                cfg_v["output_video"] = video.stem + OUTPUT_SUFFIX
                result = {}

                def cb_log(msg, level="info"):
                    self.log(msg, level)

                def cb_progress(cur, seg_total, text):
                    self.ui_queue.put(("segment_progress", cur, seg_total, text))

                pipeline = Pipeline(cfg_v, {
                    "log": cb_log, "progress": cb_progress,
                    "stop": self.stop_event,
                    "done": lambda ok, msg: result.update(ok=ok, msg=msg),
                })
                pipeline.run()
                ok = bool(result.get("ok", False))
                self.log("视频结果：%s -> %s" % (video.name, "成功" if ok else "失败：%s" % result.get("msg", "")),
                         "ok" if ok else "error")
                self.ui_queue.put(("video_done", video.name, ok, result.get("msg", "")))
                if ok and self.cfg.get("clean_output") in ("own", "all"):
                    cleanup_comfy_output(self.cfg["clean_output"], pipeline.generated_outputs,
                                         self.cfg["comfy_output_dir"], APP_DIR, self.log)
                if self.stop_event.is_set():
                    break
            self.ui_queue.put(("batch_done", total))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _stop(self):
        if messagebox.askyesno("确认停止", "停止后当前正在生成的片段会被中断，剩余视频将不再处理。继续吗？"):
            self.stop_event.set()
            self.log("正在停止…", "warn")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("退出", "任务仍在运行，确定要退出吗？"):
                return
            self.stop_event.set()
        self.save_config()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.0)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
