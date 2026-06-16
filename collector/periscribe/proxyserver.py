"""proxyserver — Claude API 프록시 '서버 본체'의 독립 실행 엔트리(periscribe-proxy.exe).

사용자가 직접 더블클릭/CLI 로 띄우는 독립 프로그램이다. 컬렉터가 spawn/kill 하지 않는다.
지금은 각 머신에서 로컬(127.0.0.1)로, 나중엔 중앙 서버 1대에서 동일 바이너리로 실행한다.
하는 일: 자체 CA/리프 인증서 보장 → apiproxy 리버스 프록시 serve(트래픽 가로채·차단·게이팅·로깅).
머신의 라우팅(ANTHROPIC_BASE_URL/CA)을 거는 건 컬렉터(머신 에이전트)의 몫 — 여기선 안 한다.

차단/게이팅 정책은 proxy-policy.json(핫리로드). 표준 라이브러리 + cryptography(인증서)."""

from __future__ import annotations

import argparse
import re
import sys
import threading
from pathlib import Path

from . import proxyguard
from .config import Config


def _data_dir() -> Path:
    return proxyguard.data_dir()


def _installed_config_path() -> Path:
    return _data_dir() / "config.json"


def _spool_path(cfg) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", cfg.machine_id or "host") or "host"
    return Path(cfg.watch_dir) / "_apilog" / f"{safe}.jsonl"


def _policy_path() -> Path:
    return _data_dir() / "proxy-policy.json"


def serve(config_path: str, logger=None) -> None:
    """블로킹: 인증서 보장 후 apiproxy 를 serve_forever. (UI 스레드/콘솔 양쪽에서 호출.)"""
    from . import apiproxy, proxycert
    cfg = Config.load(config_path)
    certs = proxycert.ensure_certs(_data_dir())
    spool = _spool_path(cfg)
    policy = _policy_path()
    log = logger or (lambda m: print(m, file=sys.stderr, flush=True))
    bind = getattr(cfg, "api_proxy_bind", "127.0.0.1") or "127.0.0.1"
    apiproxy.run_proxy(cfg.machine_id, cfg.api_proxy_port, str(spool), str(policy),
                       certs["server_pem"], certs["server_key"], logger=log, bind_host=bind)


def _run_console(config_path: str) -> int:
    """UI 미가용(또는 --no-ui): 포그라운드 콘솔에서 serve. Ctrl+C 로 종료."""
    cfg = Config.load(config_path)
    print(f"[periscribe-proxy] 프록시 서버 시작 — https://127.0.0.1:{cfg.api_proxy_port}")
    print(f"[periscribe-proxy] 정책: {_policy_path()}  (편집 시 즉시 반영)")
    print("[periscribe-proxy] 종료: 이 창을 닫거나 Ctrl+C")
    try:
        serve(config_path)
    except KeyboardInterrupt:
        print("\n[periscribe-proxy] 종료합니다.")
    return 0


def _run_ui(config_path: str) -> int:
    """최소 상태창: '실행 중' 표시 + 정지 버튼. 서버는 데몬 스레드. 창 닫기 = 서버 종료(독립 프로그램이므로)."""
    try:
        import customtkinter as ctk
    except Exception:
        return _run_console(config_path)

    cfg = Config.load(config_path)
    err: dict = {}

    def _serve_bg() -> None:
        try:
            serve(config_path)
        except Exception as e:  # noqa: BLE001
            err["msg"] = str(e)

    threading.Thread(target=_serve_bg, daemon=True).start()

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    ACCENT, ACCENT_H, INK, MUTED, OKC = "#6ea8fe", "#5a93e6", "#0b0d11", "#8a93a6", "#4cd585"
    app = ctk.CTk()
    app.title("Periscribe 프록시 서버")
    app.resizable(False, False)
    app.configure(fg_color="#0f1115")
    card = ctk.CTkFrame(app, corner_radius=16, fg_color="#171a21")
    card.pack(padx=18, pady=18, fill="both", expand=True)
    pad = {"padx": 26}
    ctk.CTkLabel(card, text="🛰  프록시 서버 실행 중",
                 font=ctk.CTkFont(size=18, weight="bold"), text_color=OKC).pack(anchor="w", pady=(22, 2), **pad)
    ctk.CTkLabel(card, text=f"https://127.0.0.1:{cfg.api_proxy_port}  ·  머신 {cfg.machine_id}",
                 text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 4), **pad)
    ctk.CTkLabel(card, text="이 창을 닫으면 서버가 정지됩니다.\n켜고 끄기(라우팅)는 컬렉터에서 합니다.",
                 text_color=MUTED, font=ctk.CTkFont(size=11), justify="left").pack(anchor="w", pady=(0, 14), **pad)
    ctk.CTkButton(card, text="정지하고 닫기", height=40, corner_radius=10, command=app.destroy,
                  font=ctk.CTkFont(size=14, weight="bold"),
                  fg_color=ACCENT, hover_color=ACCENT_H, text_color=INK).pack(fill="x", pady=(4, 22), **pad)
    app.update_idletasks()
    x = (app.winfo_screenwidth() - app.winfo_width()) // 2
    y = (app.winfo_screenheight() - app.winfo_height()) // 3
    app.geometry(f"+{x}+{y}")
    app.mainloop()
    # 창 닫힘 → 즉시 종료(데몬 서버 스레드 함께 종료). Tk 로드 onefile 의 _MEI 정리 팝업 회피.
    if getattr(sys, "frozen", False):
        sys.stdout.flush() if sys.stdout else None
        sys.exit(0)
    return 0


def main(argv=None) -> int:
    if sys.stdout is None:
        import os
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        import os
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="periscribe-proxy", description="Claude API 프록시 서버(독립 실행)")
    p.add_argument("-c", "--config", default=str(_installed_config_path()))
    p.add_argument("--no-ui", action="store_true", help="상태창 없이 콘솔에서 실행")
    a = p.parse_args(argv)
    if a.no_ui or not getattr(sys, "frozen", False):
        return _run_console(a.config)
    return _run_ui(a.config)
