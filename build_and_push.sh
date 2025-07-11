#!/bin/bash

# 💥 Stop script on error
set -e

# 📦 Load version info
VERSION=$(cat VERSION)

# 🧪 Ensure .env exists
if [ ! -f .env ]; then
  echo "❌ Missing .env file. Please create one with Spotify and Last.fm credentials."
  exit 1
fi

# 🔨 Check if builder exists
if ! docker buildx inspect mybuilder > /dev/null 2>&1; then
  echo "🔧 Creating Docker builder 'mybuilder'..."
  docker buildx create --name mybuilder --use
  docker buildx inspect mybuilder --bootstrap
else
  echo "🧱 Using existing Docker builder 'mybuilder'"
  docker buildx use mybuilder
fi

# 🚀 Build and push versioned image
echo "📦 Building and pushing image: krestaino/sptnr:$VERSION"
docker buildx build --platform linux/arm64,linux/amd64 \
  -t krestaino/sptnr:$VERSION . --push

# 🏷️ Build and push 'latest' tag
echo "📦 Building and pushing image: krestaino/sptnr:latest"
docker buildx build --platform linux/arm64,linux/amd64 \
  -t krestaino/sptnr:latest . --push

# 🎉 Done
echo "✅ Docker images pushed: $VERSION and latest"
