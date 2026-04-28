use axum::{
    extract::{Path as AxumPath, State},
    response::{Html, IntoResponse},
    routing::{get, post},
    Json, Router,
};
use chrono::{Datelike, Duration as ChronoDuration, FixedOffset, Timelike, Utc};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::{
    collections::{HashMap, HashSet},
    env,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::{process::Command, sync::Mutex, time};

const DEFAULT_CONFIG: &str = "/root/.nanobot/workspace/skills/notify-sidecar-rs/config.json";
const TZ_SHANGHAI_OFFSET: i32 = 8 * 3600;

#[derive(Debug, Clone, Deserialize, Serialize)]
struct AppConfig {
    nanobot_config_path: String,
    verify_url: String,
    state_file: String,
    text_chunk_max_len: Option<usize>,
    target_kind: Option<String>,
    target_id: Option<String>,
    jobs: Vec<JobConfig>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct JobConfig {
    id: String,
    name: String,
    enabled: bool,
    schedule: String,
    timezone: Option<String>,
    command: String,
    timeout_secs: Option<u64>,
}

#[derive(Debug, Clone)]
struct QqRuntime {
    app_id: String,
    secret: String,
    msg_format: String,
    target_kind: String,
    target_id: String,
}

#[derive(Debug, Clone, Default)]
struct TokenCache {
    access_token: String,
    expires_at: i64,
}

#[derive(Debug, Clone, Serialize, Default)]
struct Stats {
    total_runs: u64,
    success_runs: u64,
    silent_runs: u64,
    error_runs: u64,
    sent_messages: u64,
}

#[derive(Debug, Clone, Serialize, Default)]
struct JobStatus {
    last_started_at: Option<String>,
    last_finished_at: Option<String>,
    last_duration_ms: Option<u128>,
    last_status: Option<String>,
    last_trigger: Option<String>,
    last_sent: bool,
    last_error: Option<String>,
    last_stdout_preview: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct RuntimeSnapshot {
    started_at: String,
    now: String,
    stats: Stats,
    jobs: HashMap<String, JobStatus>,
    configured_jobs: Vec<JobConfig>,
    job_details: Vec<JobDetail>,
    target_kind: String,
    target_set: bool,
}

#[derive(Debug, Clone, Serialize)]
struct JobDetail {
    id: String,
    name: String,
    enabled: bool,
    schedule: String,
    schedule_note: String,
    timezone: String,
    command: String,
    timeout_secs: u64,
    next_runs: Vec<String>,
    status: JobStatus,
}

#[derive(Debug)]
struct RuntimeState {
    started_at: String,
    stats: Stats,
    jobs: HashMap<String, JobStatus>,
    last_run_keys: HashMap<String, String>,
    running_jobs: HashSet<String>,
    msg_seq: u64,
}

impl RuntimeState {
    fn new() -> Self {
        Self {
            started_at: now_iso(),
            stats: Stats::default(),
            jobs: HashMap::new(),
            last_run_keys: HashMap::new(),
            running_jobs: HashSet::new(),
            msg_seq: current_unix() as u64,
        }
    }
}

#[derive(Clone)]
struct AppState {
    config: AppConfig,
    qq: QqRuntime,
    http: Client,
    runtime: Arc<Mutex<RuntimeState>>,
    token: Arc<Mutex<TokenCache>>,
}

#[derive(Debug, Deserialize)]
struct VerifyResponse {
    success: bool,
    body: Option<String>,
    error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    access_token: Option<String>,
    expires_in: Option<String>,
    error: Option<String>,
    message: Option<String>,
}

#[derive(Debug, Serialize)]
struct RunResponse {
    ok: bool,
    job_id: String,
    status: String,
    sent: bool,
    duration_ms: u128,
    error: Option<String>,
}

#[derive(Debug, Clone)]
struct DeliveryResult {
    sent: bool,
    message: String,
}

#[tokio::main]
async fn main() -> anyhow_like::Result<()> {
    tracing_subscriber::fmt::init();

    let config_path =
        env::var("NOTIFY_SIDECAR_CONFIG").unwrap_or_else(|_| DEFAULT_CONFIG.to_string());
    let config: AppConfig = read_json_file(Path::new(&config_path)).await?;
    let qq = load_qq_runtime(&config).await?;
    let host = env::var("NOTIFY_SIDECAR_HOST").unwrap_or_else(|_| "0.0.0.0".to_string());
    let port: u16 = env::var("NOTIFY_SIDECAR_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8094);

    let state = Arc::new(AppState {
        config,
        qq,
        http: Client::builder().timeout(Duration::from_secs(60)).build()?,
        runtime: Arc::new(Mutex::new(RuntimeState::new())),
        token: Arc::new(Mutex::new(TokenCache::default())),
    });

    if let Some(parent) = Path::new(&state.config.state_file).parent() {
        let _ = tokio::fs::create_dir_all(parent).await;
    }

    let scheduler_state = state.clone();
    tokio::spawn(async move { scheduler_loop(scheduler_state).await });

    let app = Router::new()
        .route("/", get(index_handler))
        .route("/health", get(health_handler))
        .route("/api/status", get(status_handler))
        .route("/api/run/{id}", post(run_handler))
        .with_state(state);

    let addr: SocketAddr = format!("{}:{}", host, port).parse()?;
    tracing::info!("notify-sidecar-rs listening on {}", addr);
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}

async fn read_json_file<T: for<'de> Deserialize<'de>>(path: &Path) -> anyhow_like::Result<T> {
    let text = tokio::fs::read_to_string(path).await?;
    Ok(serde_json::from_str(&text)?)
}

async fn load_qq_runtime(config: &AppConfig) -> anyhow_like::Result<QqRuntime> {
    let cfg: Value = read_json_file(Path::new(&config.nanobot_config_path)).await?;
    let qq = cfg
        .get("channels")
        .and_then(|v| v.get("qq"))
        .ok_or_else(|| anyhow_like::err("missing channels.qq in nanobot config"))?;

    let app_id = required_str(qq, "app_id")?;
    let secret = required_str(qq, "secret")?;
    let msg_format = qq
        .get("msg_format")
        .and_then(Value::as_str)
        .unwrap_or("markdown")
        .to_string();

    let target_id = config
        .target_id
        .as_deref()
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map(ToOwned::to_owned)
        .or_else(|| {
            qq.get("allow_from")
                .and_then(Value::as_array)
                .and_then(|arr| arr.iter().find_map(Value::as_str))
                .map(ToOwned::to_owned)
        })
        .ok_or_else(|| anyhow_like::err("missing notify target_id and channels.qq.allow_from"))?;

    let target_kind = config
        .target_kind
        .as_deref()
        .unwrap_or("c2c")
        .trim()
        .to_ascii_lowercase();

    Ok(QqRuntime {
        app_id,
        secret,
        msg_format,
        target_kind,
        target_id,
    })
}

fn required_str(parent: &Value, key: &str) -> anyhow_like::Result<String> {
    let raw = parent
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| anyhow_like::err(format!("missing QQ config field: {key}")))?;
    resolve_env_placeholder(raw, key)
}

fn resolve_env_placeholder(value: &str, key: &str) -> anyhow_like::Result<String> {
    let trimmed = value.trim();
    if let Some(name) = trimmed
        .strip_prefix("${")
        .and_then(|rest| rest.strip_suffix('}'))
        .map(str::trim)
        .filter(|name| !name.is_empty())
    {
        match env::var(name) {
            Ok(v) if !v.trim().is_empty() => Ok(v.trim().to_string()),
            _ => Err(anyhow_like::err(format!(
                "missing env var {name} for QQ config field: {key}"
            ))),
        }
    } else {
        Ok(trimmed.to_string())
    }
}

async fn scheduler_loop(state: Arc<AppState>) {
    let mut ticker = time::interval(Duration::from_secs(10));
    ticker.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
    loop {
        ticker.tick().await;
        let now = shanghai_now();
        let run_key = format!(
            "{:04}{:02}{:02}{:02}{:02}",
            now.year(),
            now.month(),
            now.day(),
            now.hour(),
            now.minute()
        );
        for job in state.config.jobs.iter().filter(|j| j.enabled) {
            if !cron_matches(
                &job.schedule,
                now.minute(),
                now.hour(),
                now.day(),
                now.month(),
                now.weekday().number_from_monday(),
            ) {
                continue;
            }
            let should_run = {
                let mut runtime = state.runtime.lock().await;
                let key = format!("{}:{}", job.id, run_key);
                if runtime.last_run_keys.get(&job.id) == Some(&key)
                    || runtime.running_jobs.contains(&job.id)
                {
                    false
                } else {
                    runtime.last_run_keys.insert(job.id.clone(), key);
                    runtime.running_jobs.insert(job.id.clone());
                    true
                }
            };
            if should_run {
                let state_clone = state.clone();
                let job_clone = job.clone();
                tokio::spawn(async move {
                    let _ = run_job(state_clone, job_clone, "schedule").await;
                });
            }
        }
    }
}

async fn health_handler() -> impl IntoResponse {
    Json(json!({"ok": true, "service": "notify-sidecar-rs", "now": now_iso()}))
}

async fn status_handler(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    Json(snapshot(&state).await)
}

async fn index_handler(State(_state): State<Arc<AppState>>) -> impl IntoResponse {
    Html(
        r##"<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Notify &#x5b9a;&#x65f6;&#x4efb;&#x52a1;&#x6865;</title>
<style>
:root{--bg:#f4f0e6;--panel:#fffdf7;--text:#211f1a;--muted:#6e695d;--line:#ded5c4;--accent:#2d6a5f;--ok:#198754;--bad:#c24135;--warn:#b7791f;--shadow:0 18px 45px rgba(66,48,24,.12)}
[data-theme="dark"]{--bg:#151916;--panel:#202720;--text:#edf5ea;--muted:#aab5a8;--line:#354035;--accent:#78d0b4;--ok:#68d391;--bad:#fc8181;--warn:#f6c177;--shadow:0 18px 45px rgba(0,0,0,.28)}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(820px 460px at 0 -10%,rgba(45,106,95,.18),transparent 55%),radial-gradient(760px 420px at 100% 0,rgba(181,107,47,.16),transparent 50%),var(--bg);color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft Yahei",sans-serif}.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 36px}.hero{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px}.title{margin:0;font-size:31px;letter-spacing:-.04em}.sub{margin:8px 0 0;color:var(--muted);line-height:1.7}.toolbar{display:flex;gap:10px;flex-wrap:wrap}.btn,button{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:12px;padding:10px 13px;box-shadow:var(--shadow);font-weight:800;cursor:pointer;text-decoration:none}.btn.primary{background:var(--accent);color:#fff;border-color:transparent}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0}.stat{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:16px;box-shadow:var(--shadow)}.stat span{color:var(--muted)}.stat b{display:block;font-size:27px;margin-top:4px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);overflow:hidden}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:980px}th,td{padding:13px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-size:13px;background:rgba(120,120,100,.08);white-space:nowrap}tr:hover{background:rgba(45,106,95,.07)}code{display:inline-block;background:rgba(100,100,80,.13);border:1px solid var(--line);border-radius:8px;padding:3px 7px;color:var(--text);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:5px 9px;font-size:12px;font-weight:900;border:1px solid var(--line);white-space:nowrap}.pill.ok{color:var(--ok);background:rgba(25,135,84,.08);border-color:rgba(25,135,84,.3)}.pill.bad{color:var(--bad);background:rgba(194,65,53,.08);border-color:rgba(194,65,53,.3)}.pill.warn{color:var(--warn);background:rgba(183,121,31,.09);border-color:rgba(183,121,31,.32)}.pill.off{color:var(--muted)}.muted{color:var(--muted)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.modal{position:fixed;inset:0;background:rgba(0,0,0,.42);display:none;align-items:center;justify-content:center;padding:18px;z-index:20}.modal.show{display:flex}.dialog{width:min(880px,100%);max-height:88vh;overflow:auto;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:22px;box-shadow:0 24px 80px rgba(0,0,0,.35)}.dialogHead{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;padding:18px 18px 12px;border-bottom:1px solid var(--line)}.dialogTitle{margin:0;font-size:22px}.dialogBody{padding:16px 18px 18px;display:grid;gap:14px}.kv{display:grid;grid-template-columns:120px 1fr;gap:8px 12px}.kv span{color:var(--muted)}.block{border:1px solid var(--line);border-radius:16px;padding:13px;background:rgba(100,100,80,.08)}.block h3{margin:0 0 9px;font-size:15px}.runs{display:flex;gap:8px;flex-wrap:wrap}.copyrow{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px}.copybtn{box-shadow:none;padding:6px 9px;border-radius:9px;font-size:12px}.pre{display:block;white-space:pre-wrap;overflow:auto;max-height:220px;background:rgba(90,100,80,.12);border:1px solid var(--line);border-radius:12px;padding:10px}.foot{margin-top:14px;color:var(--muted);font-size:13px}@media(max-width:760px){.hero{display:block}.toolbar{margin-top:12px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.title{font-size:25px}.kv{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <section class="hero">
    <div>
      <h1 class="title">Notify &#x5b9a;&#x65f6;&#x4efb;&#x52a1;&#x6865;</h1>
      <p class="sub">&#x8fd9;&#x91cc;&#x662f; Nanobot &#x7684;&#x5b9a;&#x65f6;&#x4efb;&#x52a1;&#x900f;&#x660e;&#x9762;&#x677f;&#xff1a;&#x770b;&#x5f53;&#x524d;&#x6709;&#x54ea;&#x4e9b;&#x4efb;&#x52a1;&#x3001;cron &#x89c4;&#x5219;&#x3001;&#x4e0b;&#x6b21;&#x89e6;&#x53d1;&#x3001;&#x6700;&#x8fd1;&#x72b6;&#x6001;&#x548c;&#x5b9e;&#x9645;&#x6267;&#x884c;&#x547d;&#x4ee4;&#x3002;</p>
    </div>
    <div class="toolbar">
      <button class="btn primary" onclick="loadAll()">&#x5237;&#x65b0;&#x4efb;&#x52a1;</button>
      <button onclick="toggleTheme()">&#x5207;&#x6362;&#x660e;&#x6697;</button>
      <a class="btn" href="/api/status" target="_blank" rel="noopener">&#x72b6;&#x6001; JSON</a>
    </div>
  </section>
  <section class="stats" id="stats"></section>
  <section class="panel"><div class="tablewrap"><table><thead><tr><th>&#x4efb;&#x52a1;</th><th>&#x89c4;&#x5219;</th><th>&#x4e0b;&#x6b21;&#x8fd0;&#x884c;</th><th>&#x542f;&#x7528;</th><th>&#x6700;&#x8fd1;&#x72b6;&#x6001;</th><th>&#x6700;&#x8fd1;&#x5b8c;&#x6210;</th><th>&#x8be6;&#x60c5;</th></tr></thead><tbody id="rows"></tbody></table></div></section>
  <div class="foot" id="foot">&#x52a0;&#x8f7d;&#x4e2d;...</div>
</div>
<div class="modal" id="modal" onclick="if(event.target.id==='modal')closeModal()"><div class="dialog"><div class="dialogHead"><div><h2 class="dialogTitle" id="modalTitle"></h2><div class="muted" id="modalSub"></div></div><button onclick="closeModal()">&#x5173;&#x95ed;</button></div><div class="dialogBody" id="modalBody"></div></div></div>
<script>
const L={tasks:'\u4efb\u52a1\u6570',runs:'\u603b\u8fd0\u884c',sent:'\u5df2\u53d1\u9001',errors:'\u9519\u8bef',enabled:'\u542f\u7528',paused:'\u6682\u505c',none:'\u65e0',detail:'\u67e5\u770b\u8be6\u60c5',copied:'\u5df2\u590d\u5236',copy:'\u590d\u5236',loadFail:'\u52a0\u8f7d\u5931\u8d25\uff1a',lastRefresh:'\u6700\u540e\u5237\u65b0\uff1a',target:'\u76ee\u6807\uff1a',qqSet:'QQ \u76ee\u6807\u5df2\u914d\u7f6e',qqUnset:'QQ \u76ee\u6807\u672a\u914d\u7f6e',ruleNote:'\u89c4\u5219\u8bf4\u660e',timeout:'\u8d85\u65f6',lastTrigger:'\u6700\u8fd1\u89e6\u53d1',lastStart:'\u6700\u8fd1\u5f00\u59cb',lastDone:'\u6700\u8fd1\u5b8c\u6210',duration:'\u8017\u65f6',didSend:'\u662f\u5426\u53d1\u9001',yes:'\u662f',no:'\u5426',nextRuns:'\u4e0b\u6b21\u8fd0\u884c',command:'\u5b9e\u9645\u547d\u4ee4',recentError:'\u6700\u8fd1\u9519\u8bef',preview:'\u6700\u8fd1\u8f93\u51fa\u6458\u8981',noFuture:'\u672a\u6765\u4e00\u5e74\u5185\u6ca1\u6709\u5339\u914d\u65f6\u95f4\uff0c\u53ef\u80fd\u662f\u5360\u4f4d/\u624b\u52a8\u4efb\u52a1\u3002'};
const root=document.documentElement;if(localStorage.notifyTheme==='dark')root.setAttribute('data-theme','dark');let JOBS=[];
function toggleTheme(){const d=root.getAttribute('data-theme')==='dark';root.setAttribute('data-theme',d?'light':'dark');localStorage.notifyTheme=d?'light':'dark'}
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function statusPill(st){const s=st||'-';let cls=s==='sent'?'ok':(s==='error'?'bad':(s==='running'?'warn':'off'));return `<span class="pill ${cls}">${esc(s)}</span>`}
function copyText(text,btn){const done=()=>{const old=btn.textContent;btn.textContent=L.copied;setTimeout(()=>btn.textContent=old,1200)};if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(done).catch(()=>fallbackCopy(text,done))}else fallbackCopy(text,done)}
function fallbackCopy(text,done){const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.left='-9999px';document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();done&&done()}
function render(data){JOBS=data.job_details||[];const st=data.stats||{};document.getElementById('stats').innerHTML=`<div class="stat"><span>${L.tasks}</span><b>${JOBS.length}</b></div><div class="stat"><span>${L.runs}</span><b>${st.total_runs||0}</b></div><div class="stat"><span>${L.sent}</span><b style="color:var(--ok)">${st.sent_messages||0}</b></div><div class="stat"><span>${L.errors}</span><b style="color:var(--bad)">${st.error_runs||0}</b></div>`;document.getElementById('rows').innerHTML=JOBS.map(j=>`<tr><td><b>${esc(j.name)}</b><br><span class="muted mono">${esc(j.id)}</span></td><td><code>${esc(j.schedule)}</code><br><span class="muted">${esc(j.schedule_note)}</span></td><td>${(j.next_runs||[])[0]?esc(j.next_runs[0]):'<span class="muted">'+L.none+'</span>'}</td><td>${j.enabled?'<span class="pill ok">'+L.enabled+'</span>':'<span class="pill off">'+L.paused+'</span>'}</td><td>${statusPill(j.status?.last_status)}</td><td>${esc(j.status?.last_finished_at||'-')}</td><td><button onclick="openDetail('${esc(j.id)}')">${L.detail}</button></td></tr>`).join('');document.getElementById('foot').textContent=`${L.lastRefresh}${data.now||'-'}?${L.target}${data.target_kind||'-'}?${data.target_set?L.qqSet:L.qqUnset}?`}
async function loadAll(){try{const r=await fetch('/api/status',{cache:'no-store'});render(await r.json())}catch(e){document.getElementById('foot').textContent=L.loadFail+e.message}}
function openDetail(id){const j=JOBS.find(x=>x.id===id);if(!j)return;document.getElementById('modalTitle').textContent=j.name;document.getElementById('modalSub').textContent=`${j.id} ? ${j.enabled?L.enabled:L.paused} ? ${j.timezone}`;const runs=(j.next_runs||[]).length?(j.next_runs||[]).map(x=>`<span class="pill warn">${esc(x)}</span>`).join(''):`<span class="muted">${L.noFuture}</span>`;const err=j.status?.last_error?`<div class="block"><h3>${L.recentError}</h3><div class="pre">${esc(j.status.last_error)}</div></div>`:'';const preview=j.status?.last_stdout_preview?`<div class="block"><h3>${L.preview}</h3><div class="pre">${esc(j.status.last_stdout_preview)}</div></div>`:'';document.getElementById('modalBody').innerHTML=`<div class="kv"><span>Cron</span><b><code>${esc(j.schedule)}</code></b><span>${L.ruleNote}</span><b>${esc(j.schedule_note)}</b><span>${L.timeout}</span><b>${j.timeout_secs}s</b><span>${L.lastTrigger}</span><b>${esc(j.status?.last_trigger||'-')}</b><span>${L.lastStart}</span><b>${esc(j.status?.last_started_at||'-')}</b><span>${L.lastDone}</span><b>${esc(j.status?.last_finished_at||'-')}</b><span>${L.duration}</span><b>${j.status?.last_duration_ms==null?'-':j.status.last_duration_ms+' ms'}</b><span>${L.didSend}</span><b>${j.status?.last_sent?L.yes:L.no}</b></div><div class="block"><h3>${L.nextRuns}</h3><div class="runs">${runs}</div></div><div class="block"><div class="copyrow"><h3>${L.command}</h3><button class="copybtn" onclick='copyText(${JSON.stringify(j.command||'')},this)'>${L.copy}</button></div><div class="pre mono">${esc(j.command)}</div></div>${err}${preview}`;document.getElementById('modal').classList.add('show')}
function closeModal(){document.getElementById('modal').classList.remove('show')}
loadAll();setInterval(loadAll,15000);
</script>
</body>
</html>"##,
    )
}

async fn run_handler(
    State(state): State<Arc<AppState>>,
    AxumPath(id): AxumPath<String>,
) -> impl IntoResponse {
    let job = state.config.jobs.iter().find(|j| j.id == id).cloned();
    let Some(job) = job else {
        return Json(RunResponse {
            ok: false,
            job_id: id,
            status: "not_found".to_string(),
            sent: false,
            duration_ms: 0,
            error: Some("job not found".to_string()),
        });
    };
    let already_running = {
        let mut runtime = state.runtime.lock().await;
        if runtime.running_jobs.contains(&job.id) {
            true
        } else {
            runtime.running_jobs.insert(job.id.clone());
            false
        }
    };
    if already_running {
        return Json(RunResponse {
            ok: false,
            job_id: job.id,
            status: "running".to_string(),
            sent: false,
            duration_ms: 0,
            error: Some("job already running".to_string()),
        });
    }
    Json(run_job(state, job, "manual").await)
}

async fn snapshot(state: &Arc<AppState>) -> RuntimeSnapshot {
    let runtime = state.runtime.lock().await;
    RuntimeSnapshot {
        started_at: runtime.started_at.clone(),
        now: now_iso(),
        stats: runtime.stats.clone(),
        jobs: runtime.jobs.clone(),
        configured_jobs: state.config.jobs.clone(),
        job_details: build_job_details(&state.config.jobs, &runtime.jobs),
        target_kind: state.qq.target_kind.clone(),
        target_set: !state.qq.target_id.is_empty(),
    }
}

async fn run_job(state: Arc<AppState>, job: JobConfig, trigger: &str) -> RunResponse {
    let started = std::time::Instant::now();
    let started_at = now_iso();
    {
        let mut runtime = state.runtime.lock().await;
        let entry = runtime.jobs.entry(job.id.clone()).or_default();
        entry.last_started_at = Some(started_at.clone());
        entry.last_status = Some("running".to_string());
        entry.last_trigger = Some(trigger.to_string());
        entry.last_error = None;
    }

    let status: String;
    let mut sent = false;
    let mut error: Option<String> = None;
    let mut preview: Option<String> = None;

    match run_command(&job).await {
        Ok(stdout) => {
            preview = Some(make_preview(&stdout));
            if is_silent(&stdout) {
                status = "silent".to_string();
            } else {
                match deliver_output(&state, &stdout).await {
                    Ok(result) => {
                        sent = result.sent;
                        status = if result.sent {
                            "sent".to_string()
                        } else {
                            "silent".to_string()
                        };
                        if !result.message.is_empty() {
                            preview = Some(make_preview(&result.message));
                        }
                    }
                    Err(e) => {
                        status = "error".to_string();
                        error = Some(e.to_string());
                    }
                }
            }
        }
        Err(e) => {
            status = "error".to_string();
            error = Some(e.to_string());
        }
    }

    let duration_ms = started.elapsed().as_millis();
    let response = RunResponse {
        ok: status != "error",
        job_id: job.id.clone(),
        status: status.clone(),
        sent,
        duration_ms,
        error: error.clone(),
    };

    {
        let mut runtime = state.runtime.lock().await;
        runtime.running_jobs.remove(&job.id);
        runtime.stats.total_runs += 1;
        match status.as_str() {
            "sent" => {
                runtime.stats.success_runs += 1;
                runtime.stats.sent_messages += 1;
            }
            "silent" => runtime.stats.silent_runs += 1,
            "error" => runtime.stats.error_runs += 1,
            _ => runtime.stats.success_runs += 1,
        }
        let entry = runtime.jobs.entry(job.id.clone()).or_default();
        entry.last_finished_at = Some(now_iso());
        entry.last_duration_ms = Some(duration_ms);
        entry.last_status = Some(status);
        entry.last_sent = sent;
        entry.last_error = error;
        entry.last_stdout_preview = preview;
        let _ = persist_state(&state, &runtime).await;
    }

    response
}

async fn run_command(job: &JobConfig) -> anyhow_like::Result<String> {
    let timeout_secs = job.timeout_secs.unwrap_or(120).max(5);
    let mut child = Command::new("sh");
    child.arg("-lc").arg(&job.command);
    child.env("PYTHONUNBUFFERED", "1");
    let output = match time::timeout(Duration::from_secs(timeout_secs), child.output()).await {
        Ok(result) => result?,
        Err(_) => {
            return Err(anyhow_like::err(format!(
                "command timed out after {timeout_secs}s"
            )))
        }
    };
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    if !output.status.success() {
        let code = output
            .status
            .code()
            .map(|c| c.to_string())
            .unwrap_or_else(|| "signal".to_string());
        let msg = if stderr.is_empty() {
            make_preview(&stdout)
        } else {
            stderr
        };
        return Err(anyhow_like::err(format!("exit {code}: {msg}")));
    }
    Ok(stdout)
}

async fn deliver_output(state: &Arc<AppState>, raw: &str) -> anyhow_like::Result<DeliveryResult> {
    let verified = verify_content(state, raw).await?;
    let mut body = strip_silent_marker(&verified);
    if body.trim().is_empty() {
        return Ok(DeliveryResult {
            sent: false,
            message: String::new(),
        });
    }

    let (cleaned, wechat_ack) = strip_wechat_ack_marker(&body);
    body = strip_silent_marker(&cleaned).trim().to_string();
    if body.is_empty() {
        return Ok(DeliveryResult {
            sent: false,
            message: String::new(),
        });
    }

    send_qq_best_effort(state, &body).await?;
    if let Some((sub_id, entry_id)) = wechat_ack {
        let _ = ack_wechat_delivery(sub_id, entry_id).await;
    }
    if let Some(url) = extract_yage_url(&body) {
        let _ = ack_yage_delivery(&url).await;
    }

    Ok(DeliveryResult {
        sent: true,
        message: body,
    })
}

async fn verify_content(state: &Arc<AppState>, content: &str) -> anyhow_like::Result<String> {
    let res = state
        .http
        .post(&state.config.verify_url)
        .json(&json!({ "content": content }))
        .send()
        .await?;
    let status = res.status();
    let data: VerifyResponse = res.json().await?;
    if !status.is_success() || !data.success {
        return Err(anyhow_like::err(
            data.error
                .unwrap_or_else(|| format!("verify failed: {status}")),
        ));
    }
    Ok(data.body.unwrap_or_default())
}

async fn send_qq_best_effort(state: &Arc<AppState>, body: &str) -> anyhow_like::Result<()> {
    match send_qq_once(state, body).await {
        Ok(()) => Ok(()),
        Err(first_err) => {
            let max_len = state.config.text_chunk_max_len.unwrap_or(1200).max(200);
            if body.chars().count() <= max_len {
                return Err(first_err);
            }
            tracing::warn!("one-shot QQ send failed, fallback to chunks: {}", first_err);
            for chunk in split_message(body, max_len) {
                if !chunk.trim().is_empty() {
                    send_qq_once(state, &chunk).await?;
                }
            }
            Ok(())
        }
    }
}

async fn send_qq_once(state: &Arc<AppState>, content: &str) -> anyhow_like::Result<()> {
    let token = get_access_token(state).await?;
    let seq = next_msg_seq(state).await;
    let (url, payload) = {
        let kind = state.qq.target_kind.as_str();
        let url = if kind == "group" {
            format!(
                "https://api.sgroup.qq.com/v2/groups/{}/messages",
                state.qq.target_id
            )
        } else {
            format!(
                "https://api.sgroup.qq.com/v2/users/{}/messages",
                state.qq.target_id
            )
        };
        let payload = if state.qq.msg_format == "plain" {
            json!({ "msg_type": 0, "msg_seq": seq, "content": content })
        } else {
            json!({ "msg_type": 2, "msg_seq": seq, "markdown": { "content": content } })
        };
        (url, payload)
    };

    let res = state
        .http
        .post(url)
        .header("Authorization", format!("QQBot {token}"))
        .header("X-Union-Appid", &state.qq.app_id)
        .json(&payload)
        .send()
        .await?;
    let status = res.status();
    let text = res.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(anyhow_like::err(format!(
            "QQ send failed {status}: {}",
            make_preview(&text)
        )));
    }
    Ok(())
}

async fn next_msg_seq(state: &Arc<AppState>) -> u64 {
    let mut runtime = state.runtime.lock().await;
    runtime.msg_seq = runtime.msg_seq.saturating_add(1);
    runtime.msg_seq
}

async fn get_access_token(state: &Arc<AppState>) -> anyhow_like::Result<String> {
    let now = current_unix();
    {
        let cache = state.token.lock().await;
        if !cache.access_token.is_empty() && cache.expires_at > now + 60 {
            return Ok(cache.access_token.clone());
        }
    }

    let res = state
        .http
        .post("https://bots.qq.com/app/getAppAccessToken")
        .json(&json!({ "appId": state.qq.app_id, "clientSecret": state.qq.secret }))
        .send()
        .await?;
    let status = res.status();
    let data: TokenResponse = res.json().await?;
    if !status.is_success() {
        return Err(anyhow_like::err(format!("QQ token failed: {status}")));
    }
    let access_token = data.access_token.ok_or_else(|| {
        anyhow_like::err(
            data.message
                .or(data.error)
                .unwrap_or_else(|| "QQ token response missing access_token".to_string()),
        )
    })?;
    let expires_in = data
        .expires_in
        .as_deref()
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or(600);

    let mut cache = state.token.lock().await;
    cache.access_token = access_token.clone();
    cache.expires_at = now + expires_in;
    Ok(access_token)
}

async fn ack_wechat_delivery(sub_id: i64, entry_id: i64) -> anyhow_like::Result<()> {
    let path =
        PathBuf::from("/root/.nanobot/workspace/skills/wechat-rss-sidecar/wechat_push_cache.json");
    let mut obj = read_json_object(&path).await.unwrap_or_default();
    let key = format!("sub:{sub_id}");
    let prev = obj.get(&key).and_then(Value::as_i64).unwrap_or(0);
    if entry_id <= prev {
        return Ok(());
    }
    obj.insert(key, Value::from(entry_id));
    write_json_object_atomic(&path, &obj).await
}

async fn ack_yage_delivery(url: &str) -> anyhow_like::Result<()> {
    let path = PathBuf::from("/root/.nanobot/workspace/skills/news-curator/yage_cache.json");
    let mut obj = read_json_object(&path).await.unwrap_or_default();
    let prev = obj.get("last_url").and_then(Value::as_str).unwrap_or("");
    if !prev.is_empty() && !is_same_or_newer_yage_url(prev, url) {
        return Ok(());
    }
    obj.insert("last_url".to_string(), Value::from(url.to_string()));
    write_json_object_atomic(&path, &obj).await
}

async fn read_json_object(path: &Path) -> anyhow_like::Result<Map<String, Value>> {
    let text = match tokio::fs::read_to_string(path).await {
        Ok(v) => v,
        Err(_) => return Ok(Map::new()),
    };
    let value: Value = serde_json::from_str(&text)?;
    Ok(value.as_object().cloned().unwrap_or_default())
}

async fn write_json_object_atomic(
    path: &Path,
    obj: &Map<String, Value>,
) -> anyhow_like::Result<()> {
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let tmp = path.with_extension("tmp");
    let text = serde_json::to_string_pretty(obj)?;
    tokio::fs::write(&tmp, text).await?;
    tokio::fs::rename(&tmp, path).await?;
    Ok(())
}

async fn persist_state(state: &Arc<AppState>, runtime: &RuntimeState) -> anyhow_like::Result<()> {
    let snap = RuntimeSnapshot {
        started_at: runtime.started_at.clone(),
        now: now_iso(),
        stats: runtime.stats.clone(),
        jobs: runtime.jobs.clone(),
        configured_jobs: state.config.jobs.clone(),
        job_details: build_job_details(&state.config.jobs, &runtime.jobs),
        target_kind: state.qq.target_kind.clone(),
        target_set: !state.qq.target_id.is_empty(),
    };
    let path = PathBuf::from(&state.config.state_file);
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let tmp = path.with_extension("tmp");
    tokio::fs::write(&tmp, serde_json::to_string_pretty(&snap)?).await?;
    tokio::fs::rename(tmp, path).await?;
    Ok(())
}

fn build_job_details(jobs: &[JobConfig], statuses: &HashMap<String, JobStatus>) -> Vec<JobDetail> {
    jobs.iter()
        .map(|job| JobDetail {
            id: job.id.clone(),
            name: job.name.clone(),
            enabled: job.enabled,
            schedule: job.schedule.clone(),
            schedule_note: describe_schedule(&job.schedule),
            timezone: job
                .timezone
                .clone()
                .unwrap_or_else(|| "Asia/Shanghai".to_string()),
            command: job.command.clone(),
            timeout_secs: job.timeout_secs.unwrap_or(120).max(5),
            next_runs: if job.enabled {
                next_runs_for(&job.schedule, 5)
            } else {
                Vec::new()
            },
            status: statuses.get(&job.id).cloned().unwrap_or_default(),
        })
        .collect()
}

fn next_runs_for(expr: &str, count: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut t = shanghai_now() + ChronoDuration::minutes(1);
    for _ in 0..(366 * 24 * 60) {
        if cron_matches(
            expr,
            t.minute(),
            t.hour(),
            t.day(),
            t.month(),
            t.weekday().number_from_monday(),
        ) {
            out.push(t.format("%Y-%m-%d %H:%M %:z").to_string());
            if out.len() >= count {
                break;
            }
        }
        t += ChronoDuration::minutes(1);
    }
    out
}

fn describe_schedule(expr: &str) -> String {
    let fields: Vec<&str> = expr.split_whitespace().collect();
    if fields.len() != 5 {
        return "\u{89c4}\u{5219}\u{683c}\u{5f0f}\u{65e0}\u{6cd5}\u{8bc6}\u{522b}".to_string();
    }
    let minute = fields[0];
    let hour = fields[1];
    let day = fields[2];
    let month = fields[3];
    let weekday = fields[4];

    if day == "*" && month == "*" && hour == "*" {
        if let Some((start, step)) = minute.split_once('/') {
            if let (Ok(start), Ok(step)) = (start.parse::<u32>(), step.parse::<u32>()) {
                let mins: Vec<String> = (start..60)
                    .step_by(step.max(1) as usize)
                    .map(|m| format!("{:02}", m))
                    .collect();
                return format!(
                    "{}\u{6bcf}\u{5c0f}\u{65f6}\u{7b2c} {} \u{5206}\u{949f}\u{89e6}\u{53d1}",
                    weekday_text(weekday),
                    mins.join("/")
                );
            }
        }
        if let Ok(m) = minute.parse::<u32>() {
            return format!(
                "{}\u{6bcf}\u{5c0f}\u{65f6} {:02} \u{5206}\u{89e6}\u{53d1}",
                weekday_text(weekday),
                m
            );
        }
    }

    if day == "*" && month == "*" {
        if let (Ok(h), Ok(m)) = (hour.parse::<u32>(), minute.parse::<u32>()) {
            return format!(
                "{} {:02}:{:02} \u{89e6}\u{53d1}",
                weekday_text(weekday),
                h,
                m
            );
        }
    }

    format!(
        "cron: minute={}, hour={}, day={}, month={}, weekday={}",
        minute, hour, day, month, weekday
    )
}

fn weekday_text(field: &str) -> String {
    match field {
        "*" => "\u{6bcf}\u{5929}".to_string(),
        "1-5" => "\u{5468}\u{4e00}\u{81f3}\u{5468}\u{4e94}".to_string(),
        "1-4" => "\u{5468}\u{4e00}\u{81f3}\u{5468}\u{56db}".to_string(),
        "5" => "\u{5468}\u{4e94}".to_string(),
        "6,7" | "6-7" => "\u{5468}\u{516d}\u{3001}\u{5468}\u{65e5}".to_string(),
        "1" => "\u{5468}\u{4e00}".to_string(),
        "2" => "\u{5468}\u{4e8c}".to_string(),
        "3" => "\u{5468}\u{4e09}".to_string(),
        "4" => "\u{5468}\u{56db}".to_string(),
        "6" => "\u{5468}\u{516d}".to_string(),
        "7" | "0" => "\u{5468}\u{65e5}".to_string(),
        other => format!("\u{5468}\u{5b57}\u{6bb5} {} \u{65f6}", other),
    }
}

fn cron_matches(
    expr: &str,
    minute: u32,
    hour: u32,
    day: u32,
    month: u32,
    weekday_monday: u32,
) -> bool {
    let fields: Vec<&str> = expr.split_whitespace().collect();
    if fields.len() != 5 {
        return false;
    }
    field_matches(fields[0], minute)
        && field_matches(fields[1], hour)
        && field_matches(fields[2], day)
        && field_matches(fields[3], month)
        && field_matches(fields[4], weekday_monday)
}

fn field_matches(field: &str, value: u32) -> bool {
    if field == "*" {
        return true;
    }
    field
        .split(',')
        .any(|part| part_matches(part.trim(), value))
}

fn part_matches(part: &str, value: u32) -> bool {
    if part == "*" {
        return true;
    }
    if let Some((start, step)) = part.split_once('/') {
        let step = step.parse::<u32>().unwrap_or(0);
        if step == 0 {
            return false;
        }
        let start_val = if start == "*" {
            0
        } else {
            start.parse::<u32>().unwrap_or(u32::MAX)
        };
        return value >= start_val && (value - start_val) % step == 0;
    }
    if let Some((a, b)) = part.split_once('-') {
        let start = a.parse::<u32>().unwrap_or(u32::MAX);
        let end = b.parse::<u32>().unwrap_or(0);
        return value >= start && value <= end;
    }
    part.parse::<u32>().map(|v| v == value).unwrap_or(false)
}

fn shanghai_now() -> chrono::DateTime<FixedOffset> {
    let tz = FixedOffset::east_opt(TZ_SHANGHAI_OFFSET).unwrap();
    Utc::now().with_timezone(&tz)
}

fn now_iso() -> String {
    shanghai_now().format("%Y-%m-%d %H:%M:%S %:z").to_string()
}

fn current_unix() -> i64 {
    Utc::now().timestamp()
}

fn is_silent(text: &str) -> bool {
    let cleaned = strip_silent_marker(text);
    cleaned.trim().is_empty()
}

fn strip_silent_marker(text: &str) -> String {
    text.replace("(NO_OUTPUT_KEEP_SILENT)", "")
        .replace("(NOOUTPUTKEEP_SILENT)", "")
        .replace("?NO_OUTPUT_KEEP_SILENT?", "")
        .replace("?NOOUTPUTKEEP_SILENT?", "")
        .trim()
        .to_string()
}

fn strip_wechat_ack_marker(body: &str) -> (String, Option<(i64, i64)>) {
    let mut text = body.to_string();
    let mut search_from = 0;
    while let Some(rel_start) = text[search_from..].find("<!--") {
        let start = search_from + rel_start;
        let Some(rel_end) = text[start..].find("-->") else {
            break;
        };
        let end = start + rel_end + 3;
        let marker = &text[start..end];
        if marker.contains("NBACK_WECHAT") {
            let ack = parse_wechat_marker(marker);
            text.replace_range(start..end, "");
            return (text.trim().to_string(), ack);
        }
        search_from = end;
    }
    (text, None)
}

fn parse_wechat_marker(marker: &str) -> Option<(i64, i64)> {
    let mut sub_id = None;
    let mut entry_id = None;
    for token in marker.replace("-->", " ").split_whitespace() {
        if let Some(v) = token.strip_prefix("sub:") {
            sub_id = v.parse::<i64>().ok();
        }
        if let Some(v) = token.strip_prefix("entry:") {
            entry_id = v.parse::<i64>().ok();
        }
    }
    match (sub_id, entry_id) {
        (Some(s), Some(e)) if s > 0 && e > 0 => Some((s, e)),
        _ => None,
    }
}

fn extract_yage_url(body: &str) -> Option<String> {
    let needle = "https://yage-ai.kit.com/posts/";
    let start = body.find(needle)?;
    let tail = &body[start..];
    let end = tail
        .find(|c: char| c.is_whitespace() || c == ')' || c == ']' || c == '"' || c == '\'')
        .unwrap_or(tail.len());
    Some(tail[..end].trim().to_string())
}

fn is_same_or_newer_yage_url(prev: &str, candidate: &str) -> bool {
    let prev_date = extract_date(prev);
    let cand_date = extract_date(candidate);
    match (prev_date, cand_date) {
        (Some(p), Some(c)) => c >= p,
        (None, Some(_)) => true,
        (Some(_), None) => false,
        (None, None) => prev == candidate || prev.is_empty(),
    }
}

fn extract_date(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    for i in 0..bytes.len().saturating_sub(9) {
        let s = &text[i..i + 10];
        if s.as_bytes().get(4) == Some(&b'-')
            && s.as_bytes().get(7) == Some(&b'-')
            && s.chars()
                .enumerate()
                .all(|(idx, ch)| idx == 4 || idx == 7 || ch.is_ascii_digit())
        {
            return Some(s.to_string());
        }
    }
    None
}

fn split_message(text: &str, max_len: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut current = String::new();
    for line in text.lines() {
        let extra = if current.is_empty() {
            line.len()
        } else {
            line.len() + 1
        };
        if !current.is_empty() && current.chars().count() + extra > max_len {
            out.push(current.trim_end().to_string());
            current.clear();
        }
        if line.chars().count() > max_len {
            if !current.is_empty() {
                out.push(current.trim_end().to_string());
                current.clear();
            }
            let mut buf = String::new();
            for ch in line.chars() {
                if buf.chars().count() >= max_len {
                    out.push(buf);
                    buf = String::new();
                }
                buf.push(ch);
            }
            if !buf.is_empty() {
                current.push_str(&buf);
            }
        } else {
            if !current.is_empty() {
                current.push('\n');
            }
            current.push_str(line);
        }
    }
    if !current.trim().is_empty() {
        out.push(current.trim_end().to_string());
    }
    out
}

fn make_preview(text: &str) -> String {
    let cleaned = text.replace('\r', "").trim().to_string();
    let mut out = String::new();
    for ch in cleaned.chars().take(240) {
        out.push(ch);
    }
    if cleaned.chars().count() > 240 {
        out.push_str("...");
    }
    out
}

mod anyhow_like {
    use std::{error::Error as StdError, fmt};

    #[derive(Debug)]
    pub struct Error(String);

    impl fmt::Display for Error {
        fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            write!(f, "{}", self.0)
        }
    }

    impl StdError for Error {}

    pub type Result<T> = std::result::Result<T, Error>;

    pub fn err(msg: impl Into<String>) -> Error {
        Error(msg.into())
    }

    impl From<std::io::Error> for Error {
        fn from(value: std::io::Error) -> Self {
            Error(value.to_string())
        }
    }

    impl From<serde_json::Error> for Error {
        fn from(value: serde_json::Error) -> Self {
            Error(value.to_string())
        }
    }

    impl From<reqwest::Error> for Error {
        fn from(value: reqwest::Error) -> Self {
            Error(value.to_string())
        }
    }

    impl From<std::net::AddrParseError> for Error {
        fn from(value: std::net::AddrParseError) -> Self {
            Error(value.to_string())
        }
    }

    impl From<std::num::ParseIntError> for Error {
        fn from(value: std::num::ParseIntError) -> Self {
            Error(value.to_string())
        }
    }
}
