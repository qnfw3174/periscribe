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

# 컨테이너(인프라) 레벨 정책 템플릿. 호스트의 이 JSON 을 편집하면 다음 실행부터 적용된다(재빌드 불필요).
# periscribe-agent 가 읽어 docker run 플래그로 변환한다 — Claude Code 설정과 무관(에이전트가 못 끄는 계층).
# 기본값은 현행 동작과 동일(제약 없음 + base 격리).
POLICY_TEMPLATE = """{
  "version": 1,
  "workspace_writable": true,
  "writable_paths": [],
  "readonly_paths": [],
  "network": true,
  "drop_all_capabilities": true,
  "no_new_privileges": false,
  "read_only_rootfs": false,
  "memory": null,
  "cpus": null,
  "pids": null
}
"""

_KNOWN_POLICY_KEYS = {
    "version", "workspace_writable", "writable_paths", "readonly_paths",
    "network", "drop_all_capabilities", "no_new_privileges", "read_only_rootfs",
    "memory", "cpus", "pids",
}
_MEM_RE = re.compile(r"^\d+(\.\d+)?[bkmgBKMG]?$")
_CPU_RE = re.compile(r"^\d+(\.\d+)?$")

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


# ---- 컨테이너 레벨 정책 ----
def _policies_dir() -> Path:
    return _data_dir() / "policies"


def _proxy_run_args() -> tuple[list[str], list[str]]:
    """컨테이너 Claude 를 호스트 프록시(host.docker.internal)로 라우팅하는 docker run 인자 + 경고.
    반환 (docker_args, warnings). docker_args 가 비면 적용 불가(ca 없음). stdlib 만 사용.
    전제: 호스트 프록시 서버(periscribe-proxy)가 api_proxy_bind=0.0.0.0 으로 실행 중."""
    warns: list[str] = []
    port = 8077
    try:
        data = json.loads(_installed_config_path().read_text(encoding="utf-8-sig"))
        port = int(data.get("api_proxy_port") or 8077)
        if str(data.get("api_proxy_bind") or "127.0.0.1") not in ("0.0.0.0", "::"):
            warns.append('프록시 서버 api_proxy_bind 가 0.0.0.0 이 아니면 컨테이너에서 닿지 않습니다 '
                         '(config.json 에 "api_proxy_bind": "0.0.0.0" 설정 후 프록시 서버 재시작).')
    except Exception:
        warns.append("설치 컬렉터 config 를 못 읽어 기본 포트 8077 사용.")
    ca = _data_dir() / "ca.pem"
    if not ca.is_file():
        return [], ["프록시 CA(ca.pem)가 없습니다 — periscribe-proxy(프록시 서버)를 먼저 한 번 실행하세요."]
    ca_target = "/etc/periscribe-ca.pem"
    args = [
        "--add-host", "host.docker.internal:host-gateway",
        "--mount", f"type=bind,source={ca},target={ca_target},readonly",
        "-e", f"ANTHROPIC_BASE_URL=https://host.docker.internal:{port}",
        "-e", f"NODE_EXTRA_CA_CERTS={ca_target}",
    ]
    return args, warns


# 머신 전체 기본 정책 파일명. 박스 이름은 _sanitize_name 이 선두 '_'를 떼므로 이 이름과 충돌하지 않는다.
GLOBAL_POLICY_NAME = "_default.json"


def _resolve_policy_file(name: str, policy_arg: str) -> Path:
    """적용할 정책 파일. 우선순위: --policy 지정 > 박스별 <name>.json(있을 때만) >
    머신 전체 _default.json. 전체 기본은 없으면 템플릿으로 생성(머신 단위로 한 번만 편집하면
    모든 박스에 적용). 박스별 파일은 자동 생성하지 않고, 있으면 그 박스만 덮어쓴다."""
    if policy_arg:
        p = Path(policy_arg).expanduser()
        if not p.is_file():
            raise FileNotFoundError(str(p))
        return p
    pdir = _policies_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    per_box = pdir / f"{name}.json"
    if per_box.is_file():
        return per_box
    glob = pdir / GLOBAL_POLICY_NAME
    if not glob.exists():
        glob.write_text(POLICY_TEMPLATE, encoding="utf-8")
    return glob


def _path_rule_mounts(workspace: Path, rel_paths: object, readonly: bool,
                      kind: str) -> tuple[list[str], list[str]]:
    """워크스페이스 하위 경로별 ro/rw 중첩 bind 마운트를 만든다(컨테이너 레벨 세밀 제어).
    호스트 source 가 워크스페이스 안에 실재해야 적용(밖이거나 없으면 경고 후 무시)."""
    mounts: list[str] = []
    warns: list[str] = []
    if rel_paths in (None, ""):
        return mounts, warns
    if not isinstance(rel_paths, list):
        return mounts, [f"{kind} 는 경로 배열이어야 함 → 무시."]
    wsr = workspace.resolve()
    for rel in rel_paths:
        if not isinstance(rel, str) or not rel.strip():
            warns.append(f"{kind}: 빈/잘못된 경로 → 무시.")
            continue
        rel_clean = rel.strip().replace("\\", "/").strip("/")
        src = (workspace / rel_clean).resolve()
        try:
            src.relative_to(wsr)
        except ValueError:
            warns.append(f"{kind}: '{rel}' 는 워크스페이스 밖 → 무시(보안).")
            continue
        if src == wsr:
            warns.append(f"{kind}: '{rel}' 는 워크스페이스 루트 → workspace_writable 로 설정하세요.")
            continue
        if not src.exists():
            warns.append(f"{kind}: '{rel}' 가 호스트에 없음 → 무시.")
            continue
        m = f"type=bind,source={src},target=/workspace/{rel_clean}"
        if readonly:
            m += ",readonly"
        mounts += ["--mount", m]
    return mounts, warns


def _policy_to_run_args(policy: object, running_claude: bool,
                        workspace: Path) -> tuple[bool, list[str], list[str]]:
    """컨테이너 정책 → (워크스페이스 읽기전용?, 추가 docker 플래그, 경고들).
    잘못/누락 키는 launch 를 막지 않고 허용적 기본값으로 폴백하며 경고만 남긴다."""
    warnings: list[str] = []
    args: list[str] = []
    if not isinstance(policy, dict):
        return False, [], ["정책이 JSON 객체가 아니라 무시함(제약 없음)."]

    def _flag(key: str, default: bool) -> bool:
        v = policy.get(key, default)
        if isinstance(v, bool):
            return v
        if key in policy:
            warnings.append(f"'{key}'는 true/false 여야 함(받음: {v!r}) → 기본값 {default} 사용.")
        return default

    if policy.get("version", 1) != 1:
        warnings.append(f"알 수 없는 정책 version={policy.get('version')!r} — 그대로 진행.")

    ws_readonly = not _flag("workspace_writable", True)

    # 하위 경로별 예외(중첩 bind). ro 워크스페이스엔 writable_paths(쓰기 예외),
    # rw 워크스페이스엔 readonly_paths(보호)만 의미가 있다.
    if ws_readonly:
        m, w = _path_rule_mounts(workspace, policy.get("writable_paths"), False, "writable_paths")
        args += m; warnings += w
        if policy.get("readonly_paths"):
            warnings.append("readonly_paths: 워크스페이스가 이미 읽기전용이라 무의미 → 무시.")
    else:
        m, w = _path_rule_mounts(workspace, policy.get("readonly_paths"), True, "readonly_paths")
        args += m; warnings += w
        if policy.get("writable_paths"):
            warnings.append("writable_paths: 워크스페이스가 쓰기 가능이라 무의미 → 무시.")

    if not _flag("network", True):
        args.append("--network=none")
        if running_claude:
            warnings.append("network:false — 네트워크가 없어 Claude 로그인/API 호출이 실패합니다. "
                            "Claude를 쓰려면 network:true, 오프라인 점검만 하려면 --shell 사용.")

    if not _flag("drop_all_capabilities", True):
        args.append("--cap-add=ALL")
        warnings.append("drop_all_capabilities:false — 모든 capability 부여(샌드박스 약화).")

    if _flag("no_new_privileges", False):
        args.append("--security-opt=no-new-privileges:true")

    if _flag("read_only_rootfs", False):
        args += ["--read-only", "--tmpfs=/tmp"]
        warnings.append("read_only_rootfs:true — 실험적. 일부 도구가 쓰기 경로를 못 찾아 실패할 수 있음.")

    mem = policy.get("memory")
    if mem not in (None, ""):
        if isinstance(mem, str) and _MEM_RE.match(mem):
            args.append(f"--memory={mem}")
        else:
            warnings.append(f'memory={mem!r} 형식 오류(예: "2g") → 무시.')

    cpus = policy.get("cpus")
    if cpus not in (None, ""):
        if (isinstance(cpus, (int, float)) and not isinstance(cpus, bool)) or \
           (isinstance(cpus, str) and _CPU_RE.match(cpus)):
            args.append(f"--cpus={cpus}")
        else:
            warnings.append(f'cpus={cpus!r} 형식 오류(예: "2") → 무시.')

    pids = policy.get("pids")
    if pids not in (None, ""):
        if isinstance(pids, int) and not isinstance(pids, bool) and pids > 0:
            args.append(f"--pids-limit={pids}")
        else:
            warnings.append(f"pids={pids!r} 는 양의 정수여야 함 → 무시.")

    for k in policy:
        if k not in _KNOWN_POLICY_KEYS:
            warnings.append(f"알 수 없는 정책 키 '{k}' → 무시.")

    return ws_readonly, args, warnings


def _active_controls(ws_readonly: bool, args: list[str]) -> list[str]:
    """콘솔 배너용 — 활성 제어를 사람이 읽을 라벨로."""
    labels: list[str] = []
    if ws_readonly:
        labels.append("워크스페이스 읽기전용🔒")
    fixed = {"--network=none": "네트워크 차단", "--read-only": "루트FS 읽기전용",
             "--security-opt=no-new-privileges:true": "권한상승 차단", "--cap-add=ALL": "⚠모든 capability"}
    rw_paths = ro_paths = 0
    for j, arg in enumerate(args):
        if arg in fixed:
            labels.append(fixed[arg])
        elif arg.startswith("--memory="):
            labels.append("메모리 " + arg.split("=", 1)[1])
        elif arg.startswith("--cpus="):
            labels.append("CPU " + arg.split("=", 1)[1])
        elif arg.startswith("--pids-limit="):
            labels.append("PID " + arg.split("=", 1)[1])
        elif arg == "--mount" and j + 1 < len(args) and "target=/workspace/" in args[j + 1]:
            if ",readonly" in args[j + 1]:
                ro_paths += 1
            else:
                rw_paths += 1
    if rw_paths:
        labels.append(f"쓰기예외 {rw_paths}곳")
    if ro_paths:
        labels.append(f"보호경로 {ro_paths}곳")
    return labels


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
                     suffix: list[str], tty: bool, ws_readonly: bool = False,
                     extra: list[str] | None = None) -> list[str]:
    """devcontainer 와 동일한 마운트/격리로 run 인자 조립.
    Windows 경로의 드라이브 콜론이 `-v src:dst` 파싱과 충돌하므로 `--mount` 사용.
    ws_readonly=True 면 워크스페이스를 OS 레벨 읽기전용으로(정책 workspace_writable:false 구동).
    .claude 바인드는 transcript/로그인 영속을 위해 항상 쓰기 유지. extra=정책이 만든 추가 플래그."""
    ws_mount = f"type=bind,source={workspace},target=/workspace"
    if ws_readonly:
        ws_mount += ",readonly"
    return [
        runtime, "run", "--rm", "-i", *(["-t"] if tty else []),
        "--name", f"periscribe-{name}",
        "-u", "node",
        "-w", "/workspace",
        "--cap-drop=ALL",
        "--mount", ws_mount,
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
    p.add_argument("--policy", default="",
                   help="컨테이너(OS) 레벨 제어 정책 JSON 파일 경로. 생략 시 박스별 정책 파일을 "
                        "자동 생성/사용(편집해서 적용, 재빌드 불필요)")
    p.add_argument("--no-policy", action="store_true",
                   help="정책 미적용 — 기본 격리만으로 실행")
    p.add_argument("--api-key", default="",
                   help="ANTHROPIC_API_KEY 주입(생략 시 컨테이너 안에서 /login 으로 인증)")
    p.add_argument("--rebuild", action="store_true", help="에이전트 이미지를 강제로 다시 빌드")
    p.add_argument("--proxy", action="store_true",
                   help="컨테이너 Claude 를 호스트 프록시(periscribe-proxy)로 라우팅 — 로깅+통제 적용. "
                        "프록시 서버가 api_proxy_bind=0.0.0.0 으로 실행 중이어야 함")
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

    # 컨테이너 레벨 정책 → docker 플래그.
    ws_readonly, policy_args, policy_warnings = False, [], []
    policy_file: Path | None = None
    if not a.no_policy:
        try:
            policy_file = _resolve_policy_file(name, a.policy)
        except FileNotFoundError as e:
            print(f"[agent] 정책 파일을 찾을 수 없습니다: {e}", file=sys.stderr)
            return 2
        try:
            pol = json.loads(policy_file.read_text(encoding="utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            print(f"[agent] 정책 파일 JSON 오류 ({policy_file}): {e}", file=sys.stderr)
            return 2
        ws_readonly, policy_args, policy_warnings = _policy_to_run_args(
            pol, running_claude=not a.shell, workspace=workspace)

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

    # 프록시 경유(--proxy): 컨테이너 Claude 트래픽을 호스트 프록시로 라우팅(로깅+통제).
    proxy_args: list[str] = []
    if a.proxy:
        proxy_args, proxy_warns = _proxy_run_args()
        for w in proxy_warns:
            print(f"[agent] ⚠ {w}", file=sys.stderr)
        if not proxy_args:
            print("[agent] --proxy 를 적용할 수 없어 중단합니다.", file=sys.stderr)
            return 3
        print("[agent] 프록시 경유 활성화 — 컨테이너 Claude 트래픽이 호스트 프록시로 라우팅됩니다(웹 🛰 API).")

    suffix = ["bash"] if a.shell else ["claude"]
    extra: list[str] = (["-e", f"ANTHROPIC_API_KEY={a.api_key}"] if a.api_key else []) + proxy_args + policy_args
    tty = sys.stdin.isatty() and sys.stdout.isatty()

    print(f"[agent] 박스 '{name}' 시작 — workspace = {workspace}")
    print(f"[agent]   transcript → {claude_dir}  (웹에서 🐳{name})")
    if policy_file:
        controls = _active_controls(ws_readonly, policy_args)
        if a.policy:
            scope = "지정"
        elif policy_file.name == GLOBAL_POLICY_NAME:
            scope = "머신 전체 기본"
        else:
            scope = f"박스 '{name}' 전용"
        print(f"[agent]   컨테이너 정책({scope}): {', '.join(controls) if controls else '기본(제약 없음)'}")
        print(f"[agent]     [{policy_file}] — 편집해 제어. 다음 실행부터 적용(재빌드 불필요).")
    else:
        print("[agent]   컨테이너 정책: 미적용(--no-policy) — 기본 격리만")
    for w in policy_warnings:
        print(f"[agent]   ⚠ {w}", file=sys.stderr)
    if not a.shell and not a.api_key:
        print("[agent]   첫 실행이면 컨테이너 안에서 /login 으로 한 번 인증하세요(이후 자동 유지).")
    print("[agent]   종료: 컨테이너에서 exit / Ctrl-D")

    return subprocess.call(
        _docker_run_argv(runtime, name, workspace, claude_dir, suffix, tty,
                         ws_readonly=ws_readonly, extra=extra))


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
