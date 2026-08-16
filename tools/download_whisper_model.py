"""
下载 faster-whisper base 语音识别模型
用法: python tools/download_whisper_model.py [--pack]

默认: 下载并解压到 D:\\AI\\whisper-base-model\\（代码上级目录）
--pack: 额外打包 zip，方便传到远程服务器
"""
import os
import sys
import shutil

# ---- 镜像设置（国内必选）----
if "HF_ENDPOINT" not in os.environ:
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# ---- ffmpeg 设置 ----
try:
    import imageio_ffmpeg
    ffmpeg_dir = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
    if ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
except ImportError:
    print("[warn] imageio-ffmpeg not installed, pip install imageio-ffmpeg first")

# ---- 目标目录 ----
# 代码目录: D:\AI\code\tools\ -> 上级: D:\AI\
target_dir = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "whisper-base-model"
))
print(f"Target dir: {target_dir}")

if os.path.isdir(target_dir) and os.path.isfile(os.path.join(target_dir, "model.bin")):
    print("Model already exists, skip download.")
else:
    # ---- 下载模型 ----
    print("Downloading faster-whisper base model from hf-mirror.com ...")
    from huggingface_hub import snapshot_download
    cache_dir = snapshot_download("Systran/faster-whisper-base")
    print(f"HF cache: {cache_dir}")

    # ---- 提取到目标目录 ----
    os.makedirs(target_dir, exist_ok=True)
    for f in os.listdir(cache_dir):
        src = os.path.join(cache_dir, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(target_dir, f))
            print(f"  copied: {f}")

    # 验证
    from faster_whisper import WhisperModel
    model = WhisperModel(target_dir, device="cpu", compute_type="int8")
    print("Model verified OK.")

# ---- 可选打包 ----
if "--pack" in sys.argv:
    import zipfile

    out_zip = target_dir + ".zip"
    print(f"\nPacking to: {out_zip}")

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_STORED) as zf:
        for f in os.listdir(target_dir):
            full = os.path.join(target_dir, f)
            if os.path.isfile(full):
                zf.write(full, f)

    size_mb = os.path.getsize(out_zip) / 1024 / 1024
    print(f"Done. {out_zip} ({size_mb:.1f} MB)")
    print()
    print("=== 远程服务器部署 ===")
    print(f"1. 拷贝 {out_zip} 到远程服务器")
    print(f"2. 解压到代码上级目录，如 D:\\AI\\whisper-base-model\\")
    print(f"3. pip install faster-whisper imageio-ffmpeg")
    print(f"4. git pull")
