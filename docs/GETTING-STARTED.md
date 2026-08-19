# Periscribe 시작하기

> 이 문서는 **Periscribe를 직접 도입해서 쓰려는 사람**을 위한 것입니다.
> 저장소를 받은 상태에서 시작해, 자기 PC의 Claude Code 활동이 웹 화면에 뜨는 데까지 갑니다.
>
> 회사에서 **디바이스 토큰을 받아 설치만** 하면 되는 분은 이 문서가 아니라
> [`manual.html`](./manual.html) 2장을 보세요. 5분이면 끝납니다.

## 무엇을 하게 되나

```
① Supabase 준비   →  ② 웹 대시보드 올리기  →  ③ 암호화 키 설정  →  ④ 내 PC에 설치
   (약 15분)            (약 10분)              (약 2분)           (약 5분)
```

끝나면 Claude Code로 작업할 때마다 프롬프트·응답·실행한 명령이 웹 화면에 1초 안에 나타납니다.

## 준비물

| 필요한 것 | 비고 |
|---|---|
| Supabase 계정 | 무료 플랜으로 충분합니다 |
| Vercel 계정 (또는 정적 호스팅) | 웹 대시보드용. 로컬에서 파일로 열어도 동작합니다 |
| Windows PC | 컬렉터·설치 프로그램은 Windows 전용 |
| Claude Code | 이미 설치·로그인돼 있어야 합니다 |
| Node.js | Supabase CLI 설치용 (선택 — 대시보드에서 해도 됩니다) |

> 💡 **혼자 쓰는 경우에도 서버가 필요한 이유**: Periscribe는 여러 PC를 한 화면에서 보도록
> 설계됐습니다. 혼자 1대만 쓴다면 과한 구성일 수 있으니, 그 경우 감안하고 시작하세요.

---

## ① Supabase 준비

### 1-1. 프로젝트 만들기

[supabase.com](https://supabase.com)에서 새 프로젝트를 만듭니다. 리전은 가까운 곳(예: Northeast Asia)을
고르면 지연이 줄어듭니다. **데이터베이스 비밀번호는 따로 적어두세요.**

### 1-2. 스키마 적용

좌측 **SQL Editor** → New query → `supabase/schema.sql` 내용을 통째로 붙여넣고 **Run**.

✅ **확인**: Table Editor에 `events`, `devices`, `owner_keys`, `session_catalog`,
`backfill_requests`, `delete_requests` 6개가 보입니다.

> `pg_cron` 관련 오류가 나면 Database → Extensions에서 `pg_cron`을 켜고 다시 실행하세요.
> 이건 90일 자동 정리용이라, 안 되면 그 부분만 빼고 진행해도 나머지는 정상 동작합니다.

### 1-3. ingest 함수 배포

각 PC가 데이터를 올리는 창구입니다. **이 함수 안에만** 서버 키가 존재하고, PC들은 자기 토큰만
가집니다. 그래서 PC 하나가 털려도 다른 PC의 기록은 안전합니다.

```bash
npm install -g supabase
supabase login
supabase link --project-ref <프로젝트-ref>
supabase functions deploy ingest --no-verify-jwt
```

> ⚠ **`--no-verify-jwt`를 빠뜨리지 마세요.** 이걸 빼면 컬렉터의 모든 요청이 401로 거부되는데,
> 화면상으로는 "설치는 됐는데 아무것도 안 올라옴"으로만 보여서 원인을 찾기 어렵습니다.
> 가장 흔한 실패 지점입니다.

✅ **확인**: Edge Functions 목록에 `ingest`가 있고 Verify JWT가 꺼져 있습니다.

### 1-4. 로그인 계정 만들기

Authentication → Users → **Add user**. 이메일/비밀번호를 넣고 **Auto Confirm User**를 켭니다.

그다음 Authentication → Providers → Email에서 **"Allow new users to sign up"을 끕니다.**
안 그러면 웹 주소를 아는 누구나 가입할 수 있습니다.

### 1-5. 키 두 개 복사

Project Settings → API에서 다음을 복사해둡니다.

- **Project URL** (`https://xxxx.supabase.co`)
- **anon public** 키 ← 공개돼도 되는 키입니다. RLS가 막아줍니다
- ~~service_role 키~~ ← **절대 어디에도 넣지 마세요.** 이건 함수가 자동으로 씁니다

---

## ② 웹 대시보드 올리기

### 방법 A. Vercel (권장)

1. 이 저장소를 자기 GitHub에 push
2. Vercel → Add New Project → 저장소 선택
3. **Root Directory를 `web`으로 지정**
4. 환경변수 2개 추가:
   - `SUPABASE_URL` = 아까 복사한 Project URL
   - `SUPABASE_ANON_KEY` = anon public 키
5. Deploy

### 방법 B. 로컬에서 열기 (설치 없이 시험만)

`web/config.example.js`를 `web/config.js`로 복사해 값을 채우고, `web/index.html`을 브라우저로
직접 엽니다. 빌드 과정이 없는 정적 페이지라 그냥 열립니다.

✅ **확인**: 로그인 화면이 뜨고, 1-4에서 만든 계정으로 로그인됩니다.

---

## ③ 암호화 키 설정 ← 건너뛰지 마세요

로그인하면 **🔐 암호화 설정** 창이 뜹니다. 패스프레이즈를 정하고, **복구코드를 파일로 저장**합니다.

여기서 만들어지는 것:

- 관리자 키 한 쌍 (개인키는 패스프레이즈로 잠근 채 저장됩니다)
- 이후 각 PC가 자기 암호화 키를 만들어, 이 공개키로 봉인해 올립니다

결과적으로 **서버에는 암호문만 남고, 패스프레이즈를 아는 사람만 읽을 수 있습니다.**

> 🚨 **이 단계를 하기 전에 PC에 컬렉터를 설치하면, 컬렉터가 아무것도 올리지 않습니다.**
> 고장이 아니라 의도된 동작입니다 — 암호화할 공개키가 없는데 평문을 서버로 보낼 수는 없으니까요.
> 기록은 PC에 그대로 남아 있다가, 키를 설정하면 그때부터 올라갑니다.

> 🚨 **패스프레이즈와 복구코드를 둘 다 잃으면 기록을 영원히 못 읽습니다.** 복구 수단이 없습니다.
> 설계상 그렇게 만든 것이라 개발자도 도와줄 수 없습니다.

---

## ④ 내 PC에 설치

### 4-1. 토큰 발급

웹 상단 **⚙ 머신 관리** → PC 이름 입력(예: `내-노트북`) → **+ 토큰 발급**

토큰이 한 번만 표시됩니다. 복사해두세요.

### 4-2-a. 소스로 실행 (빌드 없이 바로)

```bash
cd collector
pip install -e .
copy config.example.json config.json
```

`config.json`을 열어 두 줄을 채웁니다.

```json
{
  "ingest_url": "https://<프로젝트-ref>.supabase.co/functions/v1/ingest",
  "device_token": "<발급받은 토큰>"
}
```

```bash
python -m periscribe
```

### 4-2-b. 실행 파일로 만들기 (다른 PC에 배포할 때)

먼저 서버 주소를 알려줘야 합니다. 소스에는 주소가 들어 있지 않습니다 — 저장소를 받은 사람마다
자기 Supabase를 쓰기 때문입니다.

```powershell
copy collector\dist.example.json collector\dist.json
# dist.json 을 열어 ingest_url 채우기
.\packaging\build.ps1
```

`packaging/out/`에 설치 프로그램 3개가 생깁니다. 컬렉터만 필요하면 `periscribe-setup.exe` 하나면
됩니다. 실행 → 설치 → 토큰 붙여넣기. 관리자 권한은 필요 없습니다.

### 4-3. 동작 확인

Claude Code를 열고 아무 말이나 시켜봅니다.

✅ 웹 화면에서 확인할 것
- 상단 헬스바에 이 PC가 🟢로 표시
- 세션 피드에 방금 대화가 나타남
- 내용이 읽힘 (🔒로 보이면 잠금해제를 안 한 것 — 패스프레이즈 입력)

**여기까지 되면 완료입니다.** 이후 PC를 켤 때마다 자동으로 시작합니다.

---

## 이제 뭘 할 수 있나

기본 수집만으로도 충분히 쓸 만하지만, 필요에 따라 세 가지를 더 켤 수 있습니다.
전부 선택이고, 나중에 켜도 됩니다.

| 기능 | 무엇을 얻나 | 켜는 법 | 대가 |
|---|---|---|---|
| **API 프록시** | 실제 주고받은 API 원문 + **위험 명령 차단** | `periscribe-proxy.exe` 실행 → 패널에서 프록시 켜기 | 프로그램을 하나 더 띄워야 함 |
| **OS 실행 감사** | Claude가 실제로 띄운 프로세스 (기록이 아니라 사실) | `periscribe audit-setup` (관리자 1회) | Sysmon 설치 필요 |
| **컨테이너 격리** | Claude를 샌드박스에 가두고 권한 제한 | `periscribe-agent <폴더>` | Docker 필요 |

각각의 사용법은 [`manual.html`](./manual.html) 4~5장에 있습니다.

**위험 명령 차단**이 궁금하시면 이것만 해보세요 —
`%LOCALAPPDATA%\Periscribe\proxy-policy.json`에:

```json
{ "gate_tool_use": true, "tool_block_patterns": ["\\brm\\b"] }
```

이제 Claude가 `rm`을 실행하려 하면 **실행되기 전에** 막힙니다. 파일을 저장하는 즉시 적용되고
재시작이 필요 없습니다.

---

## 잘 안 될 때

| 증상 | 원인 | 해결 |
|---|---|---|
| 웹에 PC가 안 나타남 | 암호화 키 설정을 안 함 | ③단계 수행. 기록은 안 지워졌으니 그때부터 올라옵니다 |
| 〃 | ingest 함수의 Verify JWT가 켜짐 | `--no-verify-jwt`로 재배포 |
| 〃 | 토큰 붙여넣기 오류 | `config.json`의 `device_token` 확인 |
| 내용이 🔒로만 보임 | 브라우저 잠금해제 안 함 | 패스프레이즈 입력 |
| 컬렉터가 조용히 종료 | 설정 오류 | `%LOCALAPPDATA%\Periscribe\logs\collector.log` |
| 설치 시 "ingest 엔드포인트 미설정" | 빌드 때 주소를 안 넣음 | `dist.json` 채우고 재빌드 |
| 프록시가 안 켜짐 | 서버가 안 떠 있음 | `periscribe-proxy.exe` 먼저 실행 |
| 프록시 켰는데 API 기록 없음 | Claude 세션이 먼저 시작됨 | Claude 재시작 (최초 1회만) |

로그 위치: `%LOCALAPPDATA%\Periscribe\logs\`

---

## 알아두면 좋은 것

**비용** — Supabase 무료 플랜은 DB 500MB, 함수 호출 월 50만 건입니다. 개인이 쓰기엔 넉넉하지만,
`store_raw`를 켜면 용량이 빠르게 찹니다. 기본값은 꺼져 있고, 90일 지난 기록은 자동 정리됩니다.

**수집되지 않는 것** — 실행 중인 명령의 진행 출력은 실시간으로 안 보입니다. 명령이 끝난 뒤
결과가 한 번에 들어옵니다.

**끄고 싶을 때** — 트레이 아이콘 우클릭 → 종료. 완전히 지우려면 설정 → 앱에서 제거하면
자동 시작 해제와 데이터 삭제까지 됩니다.

**남을 감시하는 데 쓸 거라면** — 이 도구는 다른 사람의 작업을 기록할 수 있습니다. 본인 PC가
아니라면, 무엇이 기록되는지 당사자에게 알리고 동의를 받으세요. 법적으로도 그렇고, 그게 맞습니다.

---

## 더 읽을거리

| 문서 | 내용 |
|---|---|
| [manual.html](./manual.html) | 기능별 상세 사용법 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 어떻게 만들어졌나 |
| [E2EE-DESIGN.md](./E2EE-DESIGN.md) | 암호화 설계와 **그 한계** |
| [CONTAINERS.md](./CONTAINERS.md) | 컨테이너 격리 |
| [DEPLOY.md](./DEPLOY.md) | 여러 사람에게 배포할 때 |
