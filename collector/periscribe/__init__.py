"""Periscribe collector — Claude Code transcript를 읽어 Supabase에 적재한다.

표준 라이브러리만 사용한다(외부 의존성 없음). 모듈 구성:
- config:     설정 로드(JSON 파일 + 환경변수 override)
- parser:     transcript 한 줄 -> 0개 이상 정규화 이벤트
- sink:       emit(events) 추상화. 기본 구현은 Supabase(PostgREST) insert
- tailer:     파일별 오프셋 tail(새 파일/회전/트렁케이트/미완성 줄 처리)
- checkpoint: 오프셋 영속(JSON)
- collector:  위 요소를 묶는 폴링 루프
"""

__version__ = "0.2.0"
