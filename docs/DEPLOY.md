# Periscribe 배포 가이드 (멀티테넌트 서비스)

관리자별로 격리된 서비스로 운영하는 절차. 각 관리자는 **본인이 등록한 머신만** 보고,
각 PC는 **자기 디바이스 토큰만** 보유한다(service_role은 어디에도 배포되지 않음).

```
[ PC ] periscribe ──(device_token, HTTPS)──▶ [ Edge Function: ingest ]
        transcript tail                          ├ 토큰 sha256 → devices 조회 → owner 확정
        (읽기 권한 없음)                          ├ events insert (owner_id/device_id 스탬프)
                                                  └ devices.last_seen 갱신(하트비트)
                                                        │ (service_role은 함수 안에만)
[ 관리자 ] 웹 로그인 ──(anon + JWT)──▶ events/devices  RLS: owner_id = auth.uid()
```

## 보안 모델 (먼저 이해)
- **디바이스 토큰**: 각 PC가 가진 유일한 자격. 유출돼도 **그 머신의 insert만** 가능,
  데이터 읽기·타 머신 쓰기 불가. 웹에서 머신별로 발급, 분실 시 revoke.
- **service_role**: Edge Function 시크릿으로만 존재. PC·웹·깃 어디에도 없음.
- **관리자 격리**: events/devices 읽기 RLS = `owner_id = auth.uid()`. 다른 관리자 데이터 안 보임.
- **anon 키**: 공개 사이트에 노출돼도 RLS가 인증을 요구하므로 단독으론 무력.

---

## 1. Supabase (1회 설정)
1. 전용 프로젝트에서 `supabase/schema.sql` 실행 → events/devices/sessions + owner RLS.
2. **Edge Function 배포**: `supabase/functions/ingest/` 를 `verify_jwt=false`로 배포
   (Supabase CLI `supabase functions deploy ingest --no-verify-jwt`, 또는 대시보드).
   - 함수는 환경의 `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`(자동 주입)를 사용.
3. **관리자 로그인 사용자 생성**: Authentication → Users → Add user(이메일/비번, Auto Confirm).
4. **공개 가입 비활성화**: Authentication → Email → *Allow new users to sign up* 끄기.
   (관리자만 로그인 가능하게.)

## 2. Web UI 배포 (Vercel)
1. 저장소를 Vercel에 Import, **Root Directory = `web`**.
2. 환경변수 `SUPABASE_URL`, `SUPABASE_ANON_KEY` 등록(빌드 시 `generate-config.js`가 주입).
3. Deploy → 배포 URL 접속 → 관리자 계정으로 로그인.

## 3. 머신 추가 (관리자가 웹에서)
1. 로그인 후 상단 **⚙ 머신 관리** → 머신 이름 입력 → **+ 토큰 발급**.
2. 표시된 **디바이스 토큰**과 **설치 명령**을 복사(토큰은 이때 한 번만 표시됨).
   ```
   periscribe.exe install --token <발급된토큰> --url https://<project>.supabase.co/functions/v1/ingest
   ```
3. 그 PC에서 위 명령을 실행(아래 4번). 잠시 후 헬스바에 🟢로 나타남.
4. 분실/이탈 시 머신 관리에서 **revoke** → 그 토큰은 즉시 무효.

## 4. 각 PC에 설치 (Windows)
- **단일 exe(권장, 배포 시)**: 관리자에게 받은 `periscribe.exe`로 위 install 명령 실행 →
  config 작성 + 부팅 자동실행 등록(무콘솔). Python 불필요. (빌드는 `packaging/` 참고.)
- **소스 실행(개발/빌드 전)**: 저장소를 받고 `collector/`에서
  ```
  python -m periscribe --ingest-url <URL> --device-token <토큰>
  ```
  또는 `config.json`에 `ingest_url`/`device_token`을 넣고 `python -m periscribe`.

## 5. 운영
- **헬스**: 각 머신이 주기적 ingest로 `last_seen` 갱신. 웹 헬스바에서 온라인/대기 표시.
- **레닥션**: 배포 config는 `redact: true` 권장(토큰/키/비번 마스킹).
- **로그**: `log_file` 지정 시 `collector/logs/`에 기록(로테이션).
- **revoke**: 머신 관리에서 즉시. 토큰 회전 = revoke 후 새 토큰 발급·재설치.
- **용량 주의**: 보존정책(prune)은 미포함. 누적 시 Supabase 용량/함수 호출 한도 확인.

## 6. 보안 체크리스트
- [ ] ingest 함수만 service_role 사용(시크릿). PC/웹/깃엔 service_role 없음.
- [ ] 공개 가입 비활성화, 관리자 계정만 존재.
- [ ] events/devices 읽기 RLS = owner 스코핑(다른 관리자/anon 차단).
- [ ] 디바이스 토큰은 각 PC 로컬 config에만. 분실 시 revoke.
- [ ] 레닥션 ON.
