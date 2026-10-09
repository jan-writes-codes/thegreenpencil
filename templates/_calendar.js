  // ---- Calendar helpers shared by the app and the public intro page ----
  // Inlined via a Django include; the including script defines AVAIL
  // ({tid: {"YYYY-MM-DD|HH:MM": bool}}) and CUSTOM ({tid: {"YYYY-MM-DD": [times]}}).
  const TIMES = (()=>{ const out=[]; for(let m=9*60; m<=20*60; m+=15){ out.push(String(Math.floor(m/60)).padStart(2,"0")+":"+String(m%60).padStart(2,"0")); } return out; })();
{% load i18n %}{% get_current_language as CAL_LANG %}{% if CAL_LANG == "en" %}  // English names for the public intro page under /en/.
  const DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
  const DOW_FULL = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
  const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  const MONTHS_S = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
{% else %}  const DOW = ["Mo","Di","Mi","Do","Fr","Sa","So"];
  const DOW_FULL = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"];
  const MONTHS = ["Januar","Februar","März","April","Mai","Juni","Juli","August","September","Oktober","November","Dezember"];
  const MONTHS_S = ["Jan","Feb","Mär","Apr","Mai","Jun","Jul","Aug","Sep","Okt","Nov","Dez"];
{% endif %}
  function startOfWeek(d) { const x = new Date(d); const day = (x.getDay()+6)%7; x.setDate(x.getDate()-day); x.setHours(0,0,0,0); return x; }
  function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate()+n); return x; }
  function sameDay(a,b){ return a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()&&a.getDate()===b.getDate(); }
  // Local-time YYYY-MM-DD: the date key for the API and the calendar maps.
  // Date.toISOString() converts to UTC, which shifts a local-midnight date to the
  // previous day for positive-UTC-offset users.
  function ymd(d){ return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0"); }
  function toMin(t){ const [h,m]=t.split(":").map(Number); return h*60+m; }
  function fmtLong(d){ return DOW_FULL[(d.getDay()+6)%7] + ", " + MONTHS_S[d.getMonth()] + " " + d.getDate(); }
  function isCustomTime(date,time,tid){ return ((CUSTOM[tid]||{})[ymd(date)]||[]).includes(time); }
  // Availability is opt-in: every slot starts closed (a new tutor has a blank
  // calendar) and only opens when the tutor explicitly marks it open. A slot the
  // tutor added by hand (a custom time) is open by default — adding it *is* the
  // opt-in — but can still be closed again via an override.
  function slotOpen(date,time,tid){ const m=AVAIL[tid]||{}; const k=ymd(date)+"|"+time; return k in m ? m[k] : isCustomTime(date,time,tid); }
  function dayTimes(date,tid){ const extra=(CUSTOM[tid]||{})[ymd(date)]||[]; return [...new Set([...TIMES, ...extra])].sort((a,b)=>toMin(a)-toMin(b)); }
