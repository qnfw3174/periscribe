# Periscribe 참조 devcontainer — AI 에이전트(Claude Code)를 컨테이너로 격리.
# 격리 범위: 파일시스템(워크스페이스만 마운트 → 호스트 ~/.ssh·다른 레포·시스템 안 보임).
# 네트워킹/egress 제한은 이번 범위 밖(stage 1).
FROM node:22-bookworm-slim

# 에이전트 작업용 최소 도구. 필요 시 추가.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl ripgrep \
 && rm -rf /var/lib/apt/lists/*

# Claude Code 전역 설치
RUN npm install -g @anthropic-ai/claude-code

# non-root (node 이미지 기본 'node' 사용자). transcript 디렉터리 미리 생성.
USER node
RUN mkdir -p /home/node/.claude/projects
WORKDIR /workspace
