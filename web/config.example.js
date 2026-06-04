// 이 파일을 config.js 로 복사한 뒤 값을 채우세요. config.js 는 .gitignore 됩니다.
// 여기에는 anon(read-only) 키만 넣습니다 — 절대 service_role/쓰기 키를 넣지 마세요.
window.PERISCRIBE_CONFIG = {
  SUPABASE_URL: "https://YOUR-PROJECT.supabase.co",
  SUPABASE_ANON_KEY: "YOUR-ANON-KEY",
  TABLE: "events",
  // 처음 로드 시 가져올 과거 이벤트 수
  PAGE_SIZE: 200,
};
