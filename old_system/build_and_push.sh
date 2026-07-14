#!/bin/bash

# 💥 Stop on any error
set -e

# 📦 Read version number from VERSION file
VERSION=$(cat VERSION)

# 🧪 Ensure .env exists
if [ ! -f .env ]; then
  echo "❌ Missing .env file. Please create one based on .env.example."
  exit 1
fi

# 🔨 Ensure buildx builder is set up
BUILDER_NAME="mybuilder"
if ! docker buildx inspect "$BUILDER_NAME" > /dev/null 2>&1; then
  echo "🔧 Creating Docker builder '$BUILDER_NAME'..."
  docker buildx create --name "$BUILDER_NAME" --use
  docker buildx inspect "$BUILDER_NAME" --bootstrap
else
  echo "🧱 Using existing Docker builder '$BUILDER_NAME'"
  docker buildx use "$BUILDER_NAME"
fi

# 🚀 Build and push versioned image
echo "📦 Building moventura/sptnr:$VERSION..."
docker buildx build --platform linux/arm64,linux/amd64 \
  -t moventura/sptnr:"$VERSION" . --push

# 🏷️ Build and push 'latest' tag
echo "📦 Building moventura/sptnr:latest..."
docker buildx build --platform linux/arm64,linux/amd64 \
  -t moventura/sptnr:latest . --push

# 🎉 Completion message
echo "✅ Successfully pushed: moventura/sptnr:$VERSION and moventura/sptnr:latest"
