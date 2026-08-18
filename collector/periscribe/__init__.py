"""Periscribe collector — 에이전트 활동을 수집해 Supabase에 적재한다.

수집 로직은 표준 라이브러리 중심이고, E2EE·인증서에만 `cryptography` 를 쓴다.
수집 소스는 3개: transcript(claude-code) / API 프록시(api) / OS 실행 감사(os-exec).
세 소스 모두 watch_dir 아래 .jsonl 로 모이므로 같은 파이프라인을 탄다.

핵심 모듈:
- config:     설정 로드(JSON 파일 + 환경변수 override)
- parser:     transcript 한 줄 -> 0개 이상 정규화 이벤트
- sink:       emit(events) 추상화. 기본 구현은 ingest Edge Function 전송(암호화 포함)
- tailer:     파일별 오프셋 tail(새 파일/회전/트렁케이트/미완성 줄 처리)
- checkpoint: 오프셋 영속(JSON)
- collector:  위 요소를 묶는 폴링 루프 + 하트비트·DEK 부트스트랩
- crypto:     E2EE(per-device DEK 생성, RSA-OAEP 봉인, AES-GCM 암호화)
선택 모듈:
- apiproxy/apilog/proxy*: Claude↔Anthropic 트래픽 가로채기·통제·이벤트화
- audit_win:  Sysmon 기반 OS 프로세스 실행 감사(Claude 서브트리 한정)
- agent:      Docker 샌드박스 실행기(별도 exe, 표준 라이브러리만)

전체 아키텍처는 docs/ARCHITECTURE.md 참고.
"""

__version__ = "0.2.1"
