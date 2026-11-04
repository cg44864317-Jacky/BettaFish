# -*- coding: utf-8 -*-
"""
作品内容解析
- 视频：下载 → 抽音频 / 抽帧 → 语音识别 / 画面文字识别
- 图文：下载每张图片 → 画面文字识别（不需要 ffmpeg）

语音识别后端（STOCK_VIDEO_ASR_BACKEND）：
- none     不做语音识别
- command  调用外部命令（适合接入你已有的工具），STOCK_VIDEO_ASR_COMMAND 里用 {input} 表示音频文件路径，
           程序读取命令的标准输出作为识别文字。例：analyzer transcribe {input}
- openai   OpenAI 兼容的 /audio/transcriptions 接口（Whisper 或国内兼容服务）
- funasr   本地 FunASR（需 pip install funasr modelscope，建议有 GPU）

画面文字识别后端（STOCK_VIDEO_OCR_BACKEND）：none | command | rapidocr | paddleocr
依赖系统里的 ffmpeg。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Protocol

import httpx
from loguru import logger

from .settings import MonitorSettings, get_settings

DOUYIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
}


def split_command(template: str, input_path: Path) -> List[str]:
    """把命令模板拆成参数列表并填入文件路径。Windows 下不按 POSIX 规则处理反斜杠，保留 C:\\ 这类路径。"""
    if os.name == "nt":
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in shlex.split(template, posix=False)]
    else:
        parts = shlex.split(template)
    return [part.replace("{input}", str(input_path)) for part in parts]


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> str: ...


class FrameReader(Protocol):
    def read(self, image_paths: List[Path]) -> str: ...


# ---------------------------------------------------------------------- 语音识别
class NoopTranscriber:
    def transcribe(self, audio_path: Path) -> str:
        return ""


class CommandTranscriber:
    def __init__(self, template: str, timeout: int = 900):
        if "{input}" not in template:
            raise ValueError("STOCK_VIDEO_ASR_COMMAND 必须包含 {input}")
        self.template, self.timeout = template, timeout

    def transcribe(self, audio_path: Path) -> str:
        cmd = split_command(self.template, audio_path)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if r.returncode != 0:
            raise RuntimeError(f"语音识别命令失败（{r.returncode}）：{r.stderr[-300:]}")
        return r.stdout.strip()


class OpenAITranscriber:
    def __init__(self, s: MonitorSettings):
        from openai import OpenAI

        self.client = OpenAI(api_key=s.STOCK_VIDEO_ASR_API_KEY, base_url=s.STOCK_VIDEO_ASR_BASE_URL)
        self.model = s.STOCK_VIDEO_ASR_MODEL

    def transcribe(self, audio_path: Path) -> str:
        with open(audio_path, "rb") as f:
            r = self.client.audio.transcriptions.create(model=self.model, file=f, language="zh")
        return getattr(r, "text", "") or ""


class FunASRTranscriber:
    def __init__(self):
        from funasr import AutoModel  # type: ignore

        self.model = AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc", disable_update=True)

    def transcribe(self, audio_path: Path) -> str:
        res = self.model.generate(input=str(audio_path))
        return "".join(x.get("text", "") for x in res or [])


def build_transcriber(s: Optional[MonitorSettings] = None) -> Transcriber:
    s = s or get_settings()
    backend = (s.STOCK_VIDEO_ASR_BACKEND or "none").lower()
    if backend == "command":
        return CommandTranscriber(s.STOCK_VIDEO_ASR_COMMAND or "")
    if backend == "openai":
        return OpenAITranscriber(s)
    if backend == "funasr":
        return FunASRTranscriber()
    return NoopTranscriber()


# ---------------------------------------------------------------------- 画面文字识别
class NoopFrameReader:
    def read(self, image_paths: List[Path]) -> str:
        return ""


def _dedupe_lines(lines: List[str]) -> str:
    seen, out = set(), []
    for line in lines:
        line = line.strip()
        if len(line) >= 2 and line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out)


class CommandFrameReader:
    def __init__(self, template: str):
        if "{input}" not in template:
            raise ValueError("STOCK_VIDEO_OCR_COMMAND 必须包含 {input}")
        self.template = template

    def read(self, image_paths: List[Path]) -> str:
        lines: List[str] = []
        for p in image_paths:
            cmd = split_command(self.template, p)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                lines.extend(r.stdout.splitlines())
        return _dedupe_lines(lines)


class RapidOCRFrameReader:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        self.engine = RapidOCR()

    def read(self, image_paths: List[Path]) -> str:
        lines: List[str] = []
        for p in image_paths:
            result, _ = self.engine(str(p))
            lines.extend(item[1] for item in result or [])
        return _dedupe_lines(lines)


class PaddleOCRFrameReader:
    def __init__(self):
        from paddleocr import PaddleOCR  # type: ignore

        self.engine = PaddleOCR(use_angle_cls=False, lang="ch", show_log=False)

    def read(self, image_paths: List[Path]) -> str:
        lines: List[str] = []
        for p in image_paths:
            for page in self.engine.ocr(str(p)) or []:
                lines.extend(item[1][0] for item in page or [])
        return _dedupe_lines(lines)


def build_frame_reader(s: Optional[MonitorSettings] = None) -> FrameReader:
    s = s or get_settings()
    backend = (s.STOCK_VIDEO_OCR_BACKEND or "none").lower()
    if backend == "command":
        return CommandFrameReader(s.STOCK_VIDEO_OCR_COMMAND or "")
    if backend == "rapidocr":
        return RapidOCRFrameReader()
    if backend == "paddleocr":
        return PaddleOCRFrameReader()
    return NoopFrameReader()


# ---------------------------------------------------------------------- 下载与抽取
class MediaAnalyzer:
    def __init__(
        self,
        settings: Optional[MonitorSettings] = None,
        transcriber: Optional[Transcriber] = None,
        frame_reader: Optional[FrameReader] = None,
    ):
        self.s = settings or get_settings()
        self.transcriber = transcriber or build_transcriber(self.s)
        self.frame_reader = frame_reader or build_frame_reader(self.s)
        self.enabled = not (
            isinstance(self.transcriber, NoopTranscriber) and isinstance(self.frame_reader, NoopFrameReader)
        )
        if self.enabled and not shutil.which("ffmpeg"):
            # 图文作品的文字识别不需要 ffmpeg，所以只提示，处理视频时再报错
            logger.warning("未找到 ffmpeg：视频的语音/画面解析会失败（图文不受影响），请安装 ffmpeg")

    def _workdir(self, aweme_id: str) -> Path:
        base = Path(self.s.STOCK_VIDEO_MEDIA_DIR)
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"{aweme_id}_", dir=base))

    def _cleanup(self, work: Path) -> None:
        if not self.s.STOCK_VIDEO_KEEP_MEDIA:
            shutil.rmtree(work, ignore_errors=True)
        else:
            logger.debug(f"[解析] 保留媒体文件：{work}")

    def analyze_images(self, aweme_id: str, image_urls: List[str]) -> str:
        """图文作品：下载每张图片做文字识别（最多 STOCK_VIDEO_MAX_FRAMES 张），返回画面文字。"""
        if isinstance(self.frame_reader, NoopFrameReader) or not image_urls:
            return ""
        work = self._workdir(aweme_id)
        try:
            paths: List[Path] = []
            for i, url in enumerate(image_urls[: self.s.STOCK_VIDEO_MAX_FRAMES]):
                try:
                    paths.append(self.download(url, work / f"img_{i:03d}.jpg"))
                except Exception as e:  # 单张失败不影响其他图片
                    logger.debug(f"[解析] {aweme_id} 第 {i + 1} 张图片下载失败：{e}")
            return self.frame_reader.read(paths) if paths else ""
        finally:
            self._cleanup(work)

    def download(self, url: str, dest: Path) -> Path:
        with httpx.stream("GET", url, headers=DOUYIN_HEADERS, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        return dest

    @staticmethod
    def _ffmpeg(args: List[str]) -> None:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg 失败：{r.stderr[-300:]}")

    def analyze(self, aweme_id: str, video_url: Optional[str]) -> tuple[str, str]:
        """返回 (语音文字, 画面文字)。未启用解析或没有视频地址时返回空字符串。"""
        if not self.enabled or not video_url:
            return "", ""
        if not shutil.which("ffmpeg"):
            raise RuntimeError("视频解析需要 ffmpeg，请先安装（apt install ffmpeg / brew install ffmpeg）")
        work = self._workdir(aweme_id)
        try:
            video = self.download(video_url, work / "video.mp4")
            asr_text = ocr_text = ""
            if not isinstance(self.transcriber, NoopTranscriber):
                audio = work / "audio.wav"
                self._ffmpeg(["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)])
                asr_text = self.transcriber.transcribe(audio)
            if not isinstance(self.frame_reader, NoopFrameReader):
                frames_dir = work / "frames"
                frames_dir.mkdir()
                self._ffmpeg([
                    "-i", str(video), "-vf", f"fps=1/{max(1, self.s.STOCK_VIDEO_FRAME_INTERVAL)},scale=960:-2",
                    "-frames:v", str(self.s.STOCK_VIDEO_MAX_FRAMES), str(frames_dir / "f_%03d.jpg"),
                ])
                ocr_text = self.frame_reader.read(sorted(frames_dir.glob("*.jpg")))
            return asr_text, ocr_text
        finally:
            self._cleanup(work)
