// Vercel(또는 임의 정적 호스팅) 빌드 단계에서 환경변수로 config.js를 생성한다.
// config.js 자체는 .gitignore되며, 배포 시 SUPABASE_URL / SUPABASE_ANON_KEY 환경변수로 주입.
// anon 키는 공개돼도 events/machines 읽기 RLS가 authenticated 전용이라 로그인 없이는 무력.
const fs = require("fs");

const url = process.env.SUPABASE_URL || "";
const key = process.env.SUPABASE_ANON_KEY || "";

const out =
  "window.PERISCRIBE_CONFIG = {\n" +
  "  SUPABASE_URL: " + JSON.stringify(url) + ",\n" +
  "  SUPABASE_ANON_KEY: " + JSON.stringify(key) + ",\n" +
  "  TABLE: \"events\",\n" +
  "  PAGE_SIZE: 200,\n" +
  "};\n";

fs.writeFileSync("config.js", out);
console.log("generate-config.js: config.js written (" + (url ? "URL set" : "URL MISSING!") +
  ", " + (key ? "key set" : "key MISSING!") + ")");
