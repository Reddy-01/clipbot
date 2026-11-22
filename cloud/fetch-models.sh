#!/bin/sh
set -eu
mkdir -p "${CLIPBOT_TOOLS:?}"
cd "$CLIPBOT_TOOLS"
download() {
    mkdir -p "$(dirname "$1")"
    curl --fail --silent --show-error --location --retry 3 "$2" -o "$1"
}
for file in config.json tokenizer.json vocabulary.txt model.bin; do
    download "whisper-model/$file" "https://huggingface.co/Systran/faster-whisper-tiny/resolve/d90ca5fe260221311c53c58e660288d3deb8d356/$file"
done
for file in en_US-lessac-low.onnx en_US-lessac-low.onnx.json; do
    download "piper/$file" "https://huggingface.co/rhasspy/piper-voices/resolve/e9c8a39444359388adaa94255becb37a5a2b8e51/en/en_US/lessac/low/$file"
done
download "face_detection_yunet_2023mar.onnx" "https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
download "llama/qwen2.5-0.5b-instruct-q4_k_m.gguf" "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf"
sha256sum -c /tmp/clipbot/models.sha256

