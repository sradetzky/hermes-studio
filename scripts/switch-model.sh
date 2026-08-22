#!/usr/bin/env bash
# Helper: document how model switching works.
# Real switching happens by restarting your local LLM server.
# This script is just a reminder / placeholder.

set -euo pipefail

echo "Model switching strategy for this studio:"
echo ""
echo "1. All Hermes profiles (main, studio, ...) point at the SAME local OpenAI-compatible endpoint."
echo "2. To experiment with a different model, stop the current local server and start a new one"
echo "   with the desired model/quant."
echo "3. No need to edit any config.yaml files."
echo ""
echo "Example (adjust to your actual launcher):"
echo "  # stop old"
echo "  pkill -f 'llama-server|vllm|unsloth' || true"
echo "  # start new"
echo "  ./your-launch-script.sh --model /path/to/new-model.gguf --port 8000"
echo ""
echo "Done. All profiles will use the new model on next request."