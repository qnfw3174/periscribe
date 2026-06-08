"""periscribe-agent — VS Code 없이 샌드박스 컨테이너에서 Claude Code 실행.

별도 실행 파일(`periscribe-agent.exe`, console=True)로 빌드된다. 컬렉터(`periscribe.exe`)와
완전히 독립적이며 **표준 라이브러리만** 사용한다(작은 exe).

동작: `.devcontainer` 와 동일한 마운트/격리로 컨테이너를 `docker run -it` 한다.
컨테이너의 `~/.claude` 를 호스트 `<container_root>/<name>` 에 bind 하여
  (1) 로그인 토큰(.credentials.json)을 호스트에 남겨 재실행 시 재로그인 불필요,
  (2) transcript(projects/...)를 호스트 컬렉터가 그대로 수집(웹에서 🐳<name>).
컬렉터의 _container_id_for() 규약상 container_root 아래 첫 경로 세그먼트가 곧 container_id 이므로
<container_root>/<name>/projects/<proj>/<session>.jsonl → container_id=<name>.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

AGENT_IMAGE = "periscribe-agent:latest"

# .devcontainer/Dockerfile 과 동일 내용(임베드). COPY 가 없어 추가 빌드 컨텍스트 파일 불필요.
DOCKERFILE_TEXT = """FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl ripgrep && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
USER node
RUN mkdir -p /home/node/.claude/projects
WORKDIR /workspace
"""

# MVP는 docker. podman 은 PATH 에 있으면 자동 폴백(향후 확장 지점).
_RUNTIMES = ("docker", "podman")


# ---- 경로(컬렉터 설치본과 동일 규약. 컬렉터 코드 import 안 함 = 의존성 가볍게 유지) ----
def _data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Periscribe"


def _installed_config_path() -> Path:
    return _data_dir() / "config.json"


def _agent_container_root() -> Path:
    """설치 컬렉터 config 의 container_root(있으면) 아래에 transcript 를 둔다.
    그래야 이미 그걸 감시 중인 컬렉터가 자동 수집한다. 없으면 기본 규약 경로."""
    try:
        data = json.loads(_installed_config_path().read_text(encoding="utf-8-sig"))
        cr = (data.get("container_root") or "").strip()
        if cr:
            return Path(cr)
    except Exception:
        pass
    return Path.home() / "periscribe-agents"


# ---- 런타임 ----
def _find_runtime() -> str | None:
    for exe in _RUNTIMES:
        p = shutil.which(exe)
        if p:
            return p
    return None


def _runtime_ready(runtime: str) -> tuple[bool, str]:
    """데몬이 응답하는지 확인. 미설치 vs 데몬 꺼짐을 구분해 안내한다."""
    try:
        r = subprocess.run([runtime, "info"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=25)
    except FileNotFoundError:
        return False, "컨테이너 런타임 실행 파일을 찾을 수 없습니다."
    except subprocess.TimeoutExpired:
        return False, "Docker 데몬 응답이 없습니다(Docker Desktop 기동 중일 수 있음 — 잠시 후 재시도)."
    except Exception as e:  # noqa: BLE001
        return False, f"런타임 확인 실패: {e}"
    if r.returncode != 0:
        return False, "Docker 데몬이 실행 중이 아닙니다. Docker Desktop을 켜고 다시 시도하세요."
    return True, ""


# ---- 이름/이미지/컨텍스트 ----
def _sanitize_name(raw: str) -> str:
    """Docker 컨테이너명·container_id(웹 표시) 공용. [a-z0-9_.-] 만 허용."""
    s = re.sub(r"[^a-z0-9_.-]+", "-", (raw or "").strip().lower())
    s = s.strip("-._")
    return s or "agent"


def _write_build_context() -> Path:
    ctx = _data_dir() / "agent-build"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "Dockerfile").write_text(DOCKERFILE_TEXT, encoding="utf-8")
    return ctx


def _image_exists(runtime: str) -> bool:
    r = subprocess.run([runtime, "image", "inspect", AGENT_IMAGE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def _collector_running() -> bool:
    """설치 컬렉터 로그가 최근(<2분) 갱신됐는지로 근사(경고용)."""
    try:
        log = _data_dir() / "logs" / "collector.log"
        return (time.time() - log.stat().st_mtime) < 120
    except Exception:
        return False


def _docker_run_argv(runtime: str, name: str, workspace: Path, claude_dir: Path,
                     suffix: list[str], tty: bool, extra: list[str] | None = None) -> list[str]:
    """devcontainer 와 동일한 마운트/격리로 run 인자 조립.
    Windows 경로의 드라이브 콜론이 `-v src:dst` 파싱과 충돌하므로 `--mount` 사용."""
    return [
        runtime, "run", "--rm", "-i", *(["-t"] if tty else []),
        "--name", f"periscribe-{name}",
        "-u", "node",
        "-w", "/workspace",
        "--cap-drop=ALL",
        "--mount", f"type=bind,source={workspace},target=/workspace",
        "--mount", f"type=bind,source={claude_dir},target=/home/node/.claude",
        *(extra or []),
        AGENT_IMAGE,
        *suffix,
    ]


# ---- 메인 ----
def cmd_agent(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="periscribe-agent",
        description="VS Code 없이 샌드박스 컨테이너에서 Claude Code 실행")
    p.add_argument("workspace", nargs="?", default=".",
                   help="컨테이너 /workspace 로 마운트할 작업 폴더(기본: 현재 폴더)")
    p.add_argument("--name", default="",
                   help="박스 이름 = container_id(웹 🐳 표시). 기본: 작업 폴더명")
    p.add_argument("--shell", action="store_true", help="claude 대신 bash 셸로 진입")
    p.add_argument("--api-key", default="",
                   help="ANTHROPIC_API_KEY 주입(생략 시 컨테이너 안에서 /login 으로 인증)")
    p.add_argument("--rebuild", action="store_true", help="에이전트 이미지를 강제로 다시 빌드")
    a = p.parse_args(argv)

    runtime = _find_runtime()
    if not runtime:
        print("[agent] Docker(또는 podman)를 찾을 수 없습니다. Docker Desktop을 설치하세요:\n"
              "        https://www.docker.com/products/docker-desktop/", file=sys.stderr)
        return 3
    ok, msg = _runtime_ready(runtime)
    if not ok:
        print(f"[agent] {msg}", file=sys.stderr)
        return 3

    workspace = Path(a.workspace).resolve()
    if not workspace.is_dir():
        print(f"[agent] 작업 폴더가 없습니다: {workspace}", file=sys.stderr)
        return 2

    name = _sanitize_name(a.name or workspace.name)
    claude_dir = _agent_container_root() / name
    claude_dir.mkdir(parents=True, exist_ok=True)  # bind source 는 실행 전 존재해야 함
    ctx = _write_build_context()

    # 잔재(크래시한 동명 컨테이너) 정리 — --name 충돌 방지(best-effort).
    subprocess.run([runtime, "rm", "-f", f"periscribe-{name}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not _collector_running():
        print("[agent] ⚠ 컬렉터가 감지되지 않습니다 — 세션을 웹에서 보려면 "
              "periscribe 컬렉터가 실행 중이어야 합니다(설치/로그인).", file=sys.stderr)

    if a.rebuild or not _image_exists(runtime):
        print(f"[agent] 이미지 빌드 중 ({AGENT_IMAGE}) — 최초 1회, 수 분 소요…")
        bargs = [runtime, "build", "-t", AGENT_IMAGE]
        if a.rebuild:
            bargs.append("--no-cache")
        bargs.append(str(ctx))
        rc = subprocess.call(bargs)
        if rc != 0:
            print(f"[agent] 이미지 빌드 실패(코드 {rc}).", file=sys.stderr)
            return rc

    suffix = ["bash"] if a.shell else ["claude"]
    extra: list[str] = ["-e", f"ANTHROPIC_API_KEY={a.api_key}"] if a.api_key else []
    tty = sys.stdin.isatty() and sys.stdout.isatty()

    print(f"[agent] 박스 '{name}' 시작 — workspace = {workspace}")
    print(f"[agent]   transcript → {claude_dir}  (웹에서 🐳{name})")
    if not a.shell and not a.api_key:
        print("[agent]   첫 실행이면 컨테이너 안에서 /login 으로 한 번 인증하세요(이후 자동 유지).")
    print("[agent]   종료: 컨테이너에서 exit / Ctrl-D")

    return subprocess.call(
        _docker_run_argv(runtime, name, workspace, claude_dir, suffix, tty, extra))


def _make_output_safe() -> None:
    """cp949 등 비UTF-8 콘솔에서 이모지(🐳·⚠) 출력 시 인코딩 크래시 방지."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def main_agent(argv: list[str] | None = None) -> int:
    _make_output_safe()
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return cmd_agent(argv)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"[agent] 오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main_agent())
