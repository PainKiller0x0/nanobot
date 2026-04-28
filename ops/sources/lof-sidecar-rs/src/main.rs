use std::{
    collections::HashMap,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

use axum::{
    body::{Body, Bytes},
    extract::{Path as AxumPath, State},
    http::{header, HeaderMap, Method, StatusCode, Uri},
    response::{Html, IntoResponse, Response},
    routing::{any, get, post},
    Json, Router,
};
use chrono::{DateTime, Datelike, Duration as ChronoDuration, FixedOffset, Timelike, Utc};
use futures::{stream, StreamExt};
use reqwest::Client;
use scraper::{Html as ScraperHtml, Selector};
use serde::{Deserialize, Serialize};
use tokio::{net::TcpStream, process::Command, sync::Mutex};

const DEFAULT_COST: f64 = 0.0153;
const PREMIUM_THRESHOLD: f64 = 0.05;
const AMOUNT_THRESHOLD: f64 = 500_000.0;
const LIMIT_THRESHOLD: f64 = 100.0;
const CONSECUTIVE_DAYS: i64 = 3;

const QDII_CODES: [&str; 40] = [
    "159605", "159607", "159612", "159632", "159655", "159659", "159660", "159941", "160140",
    "160216", "160416", "160719", "160723", "161116", "161125", "161126", "161127", "161128",
    "161129", "161130", "161815", "162411", "162415", "162719", "163208", "164701", "164824",
    "164906", "165513", "501018", "513030", "513050", "513080", "513100", "513110", "513290",
    "513300", "513390", "513500", "513650",
];

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SidecarStats {
    total_runs: u64,
    success_runs: u64,
    timeout_runs: u64,
    error_runs: u64,
}

impl Default for SidecarStats {
    fn default() -> Self {
        Self {
            total_runs: 0,
            success_runs: 0,
            timeout_runs: 0,
            error_runs: 0,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct LastRun {
    tag: String,
    started_at: DateTime<Utc>,
    finished_at: DateTime<Utc>,
    duration_ms: u128,
    status: String,
    report: String,
    error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct SidecarState {
    stats: SidecarStats,
    last_run: Option<LastRun>,
    last_board: Option<BoardData>,
}

#[derive(Clone)]
struct AppState {
    script_dir: PathBuf,
    state_file: PathBuf,
    dashboard_history_file: PathBuf,
    timeout_secs: u64,
    run_lock: Arc<Mutex<()>>,
    http: Client,
}

#[derive(Debug, Deserialize)]
struct RunRequest {
    tag: Option<String>,
}

#[derive(Debug, Serialize)]
struct RunResponse {
    ok: bool,
    status: String,
    tag: String,
    duration_ms: u128,
    report: String,
    error: Option<String>,
}

#[derive(Debug, Serialize)]
struct TriggerResponse {
    queued: bool,
    tag: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ManagedSidecar {
    id: String,
    name: String,
    description: String,
    port: Option<u16>,
    unit: Option<String>,
    homepage_url: Option<String>,
    check_url: Option<String>,
    check_kind: Option<String>,
    public: bool,
    logs_command: String,
    restart_command: String,
}

#[derive(Debug, Clone, Serialize)]
struct ManagedSidecarStatus {
    id: String,
    name: String,
    description: String,
    port: Option<u16>,
    unit: Option<String>,
    homepage_url: Option<String>,
    public: bool,
    ok: bool,
    check_status: String,
    unit_status: Option<String>,
    http_code: Option<u16>,
    latency_ms: Option<u128>,
    error: Option<String>,
    active_since: Option<String>,
    recent_errors: Vec<String>,
    logs_command: String,
    restart_command: String,
}

#[derive(Debug, Clone, Serialize)]
struct SidecarManagerSummary {
    total: usize,
    healthy: usize,
    unhealthy: usize,
}

#[derive(Debug, Clone, Serialize)]
struct SidecarManagerResponse {
    now: String,
    summary: SidecarManagerSummary,
    items: Vec<ManagedSidecarStatus>,
}

#[derive(Debug, Clone)]
struct Fund {
    code: String,
    name: String,
    premium: Option<f64>,
    rt_nav: Option<f64>,
    rt_premium_pct: Option<f64>,
    latest_nav: Option<f64>,
    latest_premium_pct: Option<f64>,
    price: Option<f64>,
    change_pct: Option<f64>,
    amount: Option<f64>,
    limit: Option<f64>,
    suspended: bool,
    limit_text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct BoardPoint {
    date: String,
    premium_pct: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct BoardRow {
    code: String,
    name: String,
    rt_nav: Option<f64>,
    rt_premium_pct: Option<f64>,
    latest_nav: Option<f64>,
    latest_premium_pct: Option<f64>,
    price: Option<f64>,
    change_pct: Option<f64>,
    amount_wan: Option<f64>,
    limit_text: String,
    suspended: bool,
    consecutive_days: i64,
    history: Vec<BoardPoint>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct BoardData {
    updated_at: DateTime<Utc>,
    rows: Vec<BoardRow>,
}

#[tokio::main]
async fn main() {
    let port: u16 = std::env::var("LOF_SIDECAR_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8093);
    let timeout_secs: u64 = std::env::var("LOF_SIDECAR_TIMEOUT_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(240);
    let script_dir = std::env::var("LOF_SCRIPT_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/root/.nanobot/workspace/skills/qdii-monitor"));
    let state_file = std::env::var("LOF_SIDECAR_STATE_FILE")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            PathBuf::from(
                "/root/.nanobot/workspace/skills/qdii-monitor/lof-sidecar-rs/data/state.json",
            )
        });

    if let Some(parent) = state_file.parent() {
        let _ = tokio::fs::create_dir_all(parent).await;
    }
    let dashboard_history_file = std::env::var("DASHBOARD_HISTORY_FILE")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            state_file
                .parent()
                .unwrap_or_else(|| Path::new("."))
                .join("dashboard_history.json")
        });
    if let Some(parent) = dashboard_history_file.parent() {
        let _ = tokio::fs::create_dir_all(parent).await;
    }

    let http = Client::builder()
        .timeout(Duration::from_secs(30))
        .user_agent("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")
        .build()
        .expect("build reqwest client");

    let app_state = AppState {
        script_dir,
        state_file,
        dashboard_history_file,
        timeout_secs,
        run_lock: Arc::new(Mutex::new(())),
        http,
    };

    let history_state = app_state.clone();
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(3600));
        loop {
            tick.tick().await;
            let _ = refresh_dashboard_history(&history_state).await;
        }
    });

    let app = Router::new()
        .route("/", get(dashboard))
        .route("/lof", get(index))
        .route("/lof/", get(index))
        .route("/health", get(health))
        .route("/api/status", get(api_status))
        .route("/api/system", get(api_system))
        .route("/api/dashboard-history", get(api_dashboard_history))
        .route("/sidecars", get(sidecars_page))
        .route("/api/sidecars", get(api_sidecars))
        .route("/api/notify-jobs", get(api_notify_jobs))
        .route("/rss", any(proxy_rss_root))
        .route("/rss/", any(proxy_rss_root))
        .route("/rss/*path", any(proxy_rss_path))
        .route("/reflexio", any(proxy_reflexio_root))
        .route("/reflexio/", any(proxy_reflexio_root))
        .route("/reflexio/*path", any(proxy_reflexio_path))
        .route("/obp", any(proxy_obp_root))
        .route("/obp/", any(proxy_obp_root))
        .route("/obp/*path", any(proxy_obp_path))
        .route("/trends", any(proxy_trends_root))
        .route("/trends/", any(proxy_trends_root))
        .route("/trends/*path", any(proxy_trends_path))
        .route("/api/run", post(api_run))
        .route("/api/trigger", post(api_trigger))
        .with_state(app_state.clone());

    tokio::spawn(auto_refresh_loop(app_state.clone()));

    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    println!("lof-sidecar-rs listening on http://{}", addr);
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("bind failed");
    axum::serve(listener, app).await.expect("server failed");
}

async fn health() -> impl IntoResponse {
    Json(serde_json::json!({
        "ok": true,
        "service": "lof-sidecar-rs",
        "time": Utc::now().to_rfc3339(),
    }))
}

async fn api_status(State(state): State<AppState>) -> impl IntoResponse {
    let current = load_state(&state.state_file).await;
    Json(current)
}

async fn api_system() -> impl IntoResponse {
    Json(serde_json::json!({
        "ok": true,
        "now": shanghai_now().format("%Y-%m-%d %H:%M:%S %:z").to_string(),
        "memory": read_meminfo_mb(),
        "loadavg": read_loadavg(),
        "disk_root": read_disk_root().await,
    }))
}

async fn api_dashboard_history(State(state): State<AppState>) -> impl IntoResponse {
    Json(refresh_dashboard_history(&state).await)
}

async fn refresh_dashboard_history(state: &AppState) -> serde_json::Value {
    let now = shanghai_now();
    let day = now.format("%Y-%m-%d").to_string();
    let memory = read_meminfo_mb();
    let mem_used = json_u64(&memory, "used_mb");
    let mem_pct = json_f64(&memory, "used_pct");

    let sidecars = sidecar_manager_snapshot(state).await;
    let service_total = sidecars.summary.total as u64;
    let service_healthy = sidecars.summary.healthy as u64;
    let service_unhealthy = sidecars.summary.unhealthy as u64;

    let notify = fetch_json_value(&state.http, "http://127.0.0.1:8094/api/status")
        .await
        .unwrap_or_else(|| serde_json::json!({}));
    let jobs = notify
        .get("job_details")
        .or_else(|| notify.get("configured_jobs"))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let task_errors = jobs
        .iter()
        .filter(|j| {
            matches!(
                j.pointer("/status/last_status").and_then(|v| v.as_str()),
                Some("error") | Some("timeout")
            )
        })
        .count() as u64;
    let task_sent = jobs
        .iter()
        .filter(|j| {
            j.pointer("/status/last_sent")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
                && j.pointer("/status/last_finished_at")
                    .and_then(|v| v.as_str())
                    .is_some_and(|s| s.contains(&day))
        })
        .count() as u64;
    let task_runs = jobs
        .iter()
        .filter(|j| {
            j.pointer("/status/last_finished_at")
                .or_else(|| j.pointer("/status/last_started_at"))
                .and_then(|v| v.as_str())
                .is_some_and(|s| s.contains(&day))
        })
        .count() as u64;

    let rss = fetch_json_value(&state.http, "http://127.0.0.1:8091/api/entries?days=1&limit=100")
        .await
        .unwrap_or_else(|| serde_json::json!({}));
    let article_count = rss
        .get("items")
        .and_then(|v| v.as_array())
        .map(|items| items.len() as u64)
        .unwrap_or(0);

    let lof = load_state(&state.state_file).await;
    let lof_high = lof
        .last_board
        .as_ref()
        .map(|board| {
            board
                .rows
                .iter()
                .filter(|row| row.rt_premium_pct.unwrap_or(0.0) >= 5.0)
                .count() as u64
        })
        .unwrap_or(0);

    let mut history = read_dashboard_history(&state.dashboard_history_file).await;
    let today_sample = serde_json::json!({
        "day": day,
        "updated_at": now.format("%Y-%m-%d %H:%M:%S %:z").to_string(),
        "memory_used_mb": mem_used,
        "memory_used_max_mb": mem_used,
        "memory_used_pct": mem_pct,
        "service_healthy": service_healthy,
        "service_total": service_total,
        "service_unhealthy": service_unhealthy,
        "service_unhealthy_max": service_unhealthy,
        "task_runs": task_runs,
        "task_sent": task_sent,
        "task_errors": task_errors,
        "task_errors_max": task_errors,
        "articles": article_count,
        "lof_high_premium": lof_high,
        "lof_high_premium_max": lof_high,
    });

    if let Some(existing) = history
        .iter_mut()
        .find(|item| item.get("day").and_then(|v| v.as_str()) == Some(day.as_str()))
    {
        update_dashboard_history_entry(existing, today_sample);
    } else {
        history.push(today_sample);
    }

    history.sort_by_key(|item| {
        item.get("day")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    });
    if history.len() > 7 {
        let remove_count = history.len() - 7;
        history.drain(0..remove_count);
    }
    let _ = write_dashboard_history(&state.dashboard_history_file, &history).await;

    serde_json::json!({
        "ok": true,
        "now": now.format("%Y-%m-%d %H:%M:%S %:z").to_string(),
        "retention_days": 7,
        "items": history,
    })
}

async fn fetch_json_value(client: &Client, url: &str) -> Option<serde_json::Value> {
    let resp = tokio::time::timeout(Duration::from_secs(3), client.get(url).send())
        .await
        .ok()?
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    resp.json::<serde_json::Value>().await.ok()
}

async fn read_dashboard_history(path: &Path) -> Vec<serde_json::Value> {
    match tokio::fs::read_to_string(path).await {
        Ok(text) => serde_json::from_str::<Vec<serde_json::Value>>(&text).unwrap_or_default(),
        Err(_) => Vec::new(),
    }
}

async fn write_dashboard_history(path: &Path, history: &[serde_json::Value]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let body = serde_json::to_string_pretty(history).unwrap_or_else(|_| "[]".to_string());
    tokio::fs::write(path, format!("{body}\n")).await
}

fn update_dashboard_history_entry(existing: &mut serde_json::Value, sample: serde_json::Value) {
    let Some(obj) = existing.as_object_mut() else {
        *existing = sample;
        return;
    };
    if let Some(sample_obj) = sample.as_object() {
        for key in [
            "updated_at",
            "memory_used_mb",
            "memory_used_pct",
            "service_healthy",
            "service_total",
            "service_unhealthy",
            "task_runs",
            "task_sent",
            "task_errors",
            "articles",
            "lof_high_premium",
        ] {
            if let Some(value) = sample_obj.get(key) {
                obj.insert(key.to_string(), value.clone());
            }
        }
    }
    update_max_field(obj, "memory_used_max_mb", mem_value(&sample, "memory_used_max_mb"));
    update_max_field(obj, "service_unhealthy_max", mem_value(&sample, "service_unhealthy_max"));
    update_max_field(obj, "task_errors_max", mem_value(&sample, "task_errors_max"));
    update_max_field(
        obj,
        "lof_high_premium_max",
        mem_value(&sample, "lof_high_premium_max"),
    );
}

fn update_max_field(obj: &mut serde_json::Map<String, serde_json::Value>, key: &str, value: u64) {
    let current = obj.get(key).and_then(|v| v.as_u64()).unwrap_or(0);
    obj.insert(key.to_string(), serde_json::json!(current.max(value)));
}

fn mem_value(value: &serde_json::Value, key: &str) -> u64 {
    value.get(key).and_then(|v| v.as_u64()).unwrap_or(0)
}

fn json_u64(value: &serde_json::Value, key: &str) -> u64 {
    value.get(key).and_then(|v| v.as_u64()).unwrap_or(0)
}

fn json_f64(value: &serde_json::Value, key: &str) -> f64 {
    value.get(key).and_then(|v| v.as_f64()).unwrap_or(0.0)
}

fn read_meminfo_mb() -> serde_json::Value {
    let text = std::fs::read_to_string("/proc/meminfo").unwrap_or_default();
    let mut values: HashMap<String, u64> = HashMap::new();
    for line in text.lines() {
        let mut parts = line.split_whitespace();
        let Some(key) = parts.next() else { continue };
        let Some(raw) = parts.next() else { continue };
        if let Ok(kb) = raw.parse::<u64>() {
            values.insert(key.trim_end_matches(':').to_string(), kb / 1024);
        }
    }
    let total = values.get("MemTotal").copied().unwrap_or(0);
    let available = values.get("MemAvailable").copied().unwrap_or(0);
    let used = total.saturating_sub(available);
    let swap_total = values.get("SwapTotal").copied().unwrap_or(0);
    let swap_free = values.get("SwapFree").copied().unwrap_or(0);
    serde_json::json!({
        "total_mb": total,
        "available_mb": available,
        "used_mb": used,
        "used_pct": if total > 0 { (used as f64 * 100.0 / total as f64 * 10.0).round() / 10.0 } else { 0.0 },
        "swap_used_mb": swap_total.saturating_sub(swap_free),
        "swap_total_mb": swap_total,
    })
}

fn read_loadavg() -> serde_json::Value {
    let text = std::fs::read_to_string("/proc/loadavg").unwrap_or_default();
    let parts: Vec<&str> = text.split_whitespace().take(3).collect();
    serde_json::json!({
        "one": parts.get(0).copied().unwrap_or("-"),
        "five": parts.get(1).copied().unwrap_or("-"),
        "fifteen": parts.get(2).copied().unwrap_or("-"),
    })
}

async fn read_disk_root() -> serde_json::Value {
    let output = tokio::time::timeout(
        Duration::from_secs(2),
        Command::new("df").arg("-Pm").arg("/").output(),
    )
    .await;
    match output {
        Ok(Ok(out)) => {
            let text = String::from_utf8_lossy(&out.stdout);
            let Some(line) = text.lines().nth(1) else {
                return serde_json::json!({"ok": false});
            };
            let cols: Vec<&str> = line.split_whitespace().collect();
            if cols.len() < 6 {
                return serde_json::json!({"ok": false});
            }
            serde_json::json!({
                "ok": true,
                "total_mb": cols.get(1).and_then(|v| v.parse::<u64>().ok()).unwrap_or(0),
                "used_mb": cols.get(2).and_then(|v| v.parse::<u64>().ok()).unwrap_or(0),
                "available_mb": cols.get(3).and_then(|v| v.parse::<u64>().ok()).unwrap_or(0),
                "used_pct": cols.get(4).copied().unwrap_or("-"),
                "mount": cols.get(5).copied().unwrap_or("/"),
            })
        }
        _ => serde_json::json!({"ok": false}),
    }
}

async fn api_sidecars(State(state): State<AppState>) -> impl IntoResponse {
    Json(sidecar_manager_snapshot(&state).await)
}

macro_rules! proxy_pair {
    ($root_fn:ident, $path_fn:ident, $upstream:literal, $prefix:literal) => {
        async fn $root_fn(
            State(state): State<AppState>,
            method: Method,
            uri: Uri,
            headers: HeaderMap,
            body: Bytes,
        ) -> Response {
            reverse_proxy(state, $upstream, $prefix, "", method, uri, headers, body).await
        }

        async fn $path_fn(
            State(state): State<AppState>,
            AxumPath(path): AxumPath<String>,
            method: Method,
            uri: Uri,
            headers: HeaderMap,
            body: Bytes,
        ) -> Response {
            reverse_proxy(state, $upstream, $prefix, &path, method, uri, headers, body).await
        }
    };
}

proxy_pair!(proxy_rss_root, proxy_rss_path, "http://127.0.0.1:8091", "/rss");
proxy_pair!(
    proxy_reflexio_root,
    proxy_reflexio_path,
    "http://127.0.0.1:8081",
    "/reflexio"
);
proxy_pair!(
    proxy_trends_root,
    proxy_trends_path,
    "http://127.0.0.1:8095",
    "/trends"
);

async fn proxy_obp_root(
    State(state): State<AppState>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if !is_obp_proxy_authorized(&headers) {
        return obp_unauthorized_response();
    }
    reverse_proxy(
        state,
        "http://127.0.0.1:8000",
        "/obp",
        "",
        method,
        uri,
        headers,
        body,
    )
    .await
}

async fn proxy_obp_path(
    State(state): State<AppState>,
    AxumPath(path): AxumPath<String>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if !is_obp_proxy_authorized(&headers) {
        return obp_unauthorized_response();
    }
    reverse_proxy(
        state,
        "http://127.0.0.1:8000",
        "/obp",
        &path,
        method,
        uri,
        headers,
        body,
    )
    .await
}

fn is_obp_proxy_authorized(headers: &HeaderMap) -> bool {
    let token = std::env::var("OBP_PROXY_TOKEN")
        .unwrap_or_default()
        .trim()
        .to_string();
    if token.is_empty() {
        return true;
    }

    if headers
        .get("x-obp-token")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.trim() == token)
    {
        return true;
    }

    let Some(auth) = headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()) else {
        return false;
    };
    let auth = auth.trim();
    if auth == format!("Bearer {token}") {
        return true;
    }

    let basic_b64 = std::env::var("OBP_PROXY_BASIC_B64")
        .unwrap_or_default()
        .trim()
        .to_string();
    !basic_b64.is_empty() && auth == format!("Basic {basic_b64}")
}

fn obp_unauthorized_response() -> Response {
    Response::builder()
        .status(StatusCode::UNAUTHORIZED)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .header(header::WWW_AUTHENTICATE, r#"Basic realm="OBP""#)
        .body(Body::from("OBP requires authentication"))
        .unwrap()
}

async fn reverse_proxy(
    state: AppState,
    upstream: &'static str,
    prefix: &'static str,
    path: &str,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let upstream_path = if path.is_empty() {
        "/".to_string()
    } else {
        format!("/{}", path)
    };
    let query = uri.query().map(|q| format!("?{}", q)).unwrap_or_default();
    let url = format!("{}{}{}", upstream, upstream_path, query);

    let mut req = state.http.request(method, &url).body(body.to_vec());
    for (name, value) in headers.iter() {
        if *name == header::HOST
            || *name == header::CONNECTION
            || *name == header::CONTENT_LENGTH
            || *name == header::ACCEPT_ENCODING
        {
            continue;
        }
        req = req.header(name, value);
    }

    let Ok(resp) = req.send().await else {
        return response_with_status(StatusCode::BAD_GATEWAY, "upstream request failed");
    };
    let status = resp.status();
    let content_type = resp
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let bytes = match resp.bytes().await {
        Ok(v) => v,
        Err(_) => return response_with_status(StatusCode::BAD_GATEWAY, "upstream read failed"),
    };

    if should_rewrite_text(&content_type) {
        match String::from_utf8(bytes.to_vec()) {
            Ok(text) => {
                let rewritten = rewrite_proxy_text(text, prefix);
                return Response::builder()
                    .status(status)
                    .header(header::CONTENT_TYPE, content_type)
                    .body(Body::from(rewritten))
                    .unwrap_or_else(|_| {
                        response_with_status(StatusCode::BAD_GATEWAY, "response build failed")
                    });
            }
            Err(_) => {}
        }
    }

    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, content_type)
        .body(Body::from(bytes))
        .unwrap_or_else(|_| response_with_status(StatusCode::BAD_GATEWAY, "response build failed"))
}

fn response_with_status(status: StatusCode, body: &'static str) -> Response {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(Body::from(body))
        .unwrap()
}

fn should_rewrite_text(content_type: &str) -> bool {
    content_type.contains("text/html")
        || content_type.contains("application/javascript")
        || content_type.contains("text/javascript")
        || content_type.contains("text/css")
}

fn rewrite_proxy_text(mut text: String, prefix: &str) -> String {
    for (from, to) in [
        ("'/api", format!("'{}{}", prefix, "/api")),
        ("\"/api", format!("\"{}{}", prefix, "/api")),
        ("`/api", format!("`{}{}", prefix, "/api")),
        ("'/admin", format!("'{}{}", prefix, "/admin")),
        ("\"/admin", format!("\"{}{}", prefix, "/admin")),
        ("`/admin", format!("`{}{}", prefix, "/admin")),
        ("'/v1", format!("'{}{}", prefix, "/v1")),
        ("\"/v1", format!("\"{}{}", prefix, "/v1")),
        ("`/v1", format!("`{}{}", prefix, "/v1")),
        ("href=\"/\"", format!("href=\"{}/\"", prefix)),
        ("href='/ '".trim(), format!("href='{}/'", prefix)),
    ] {
        text = text.replace(from, &to);
    }
    text
}

async fn api_notify_jobs(State(state): State<AppState>) -> impl IntoResponse {
    match state
        .http
        .get("http://127.0.0.1:8094/api/status")
        .send()
        .await
    {
        Ok(resp) => {
            let status = resp.status();
            match resp.json::<serde_json::Value>().await {
                Ok(value) if status.is_success() => (StatusCode::OK, Json(value)),
                Ok(value) => (
                    StatusCode::BAD_GATEWAY,
                    Json(
                        serde_json::json!({"ok": false, "error": format!("notify status {}", status), "body": value}),
                    ),
                ),
                Err(e) => (
                    StatusCode::BAD_GATEWAY,
                    Json(serde_json::json!({"ok": false, "error": e.to_string()})),
                ),
            }
        }
        Err(e) => (
            StatusCode::BAD_GATEWAY,
            Json(serde_json::json!({"ok": false, "error": e.to_string()})),
        ),
    }
}

async fn dashboard() -> Html<String> {
    Html(
        r##"<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Nanobot &#x9a7e;&#x9a76;&#x8231;</title>
<style>
:root{--paper:#fffaf0;--ink:#202019;--muted:#6f695d;--line:#e5dccb;--accent:#c26a2e;--accent2:#2f7f72;--ok:#17834f;--warn:#b76a12;--bad:#c43d32;--panel:rgba(255,250,240,.86);--shadow:0 22px 70px rgba(77,54,28,.16);--glow:rgba(194,106,46,.18)}
[data-theme="dark"]{--paper:#111816;--ink:#eef4e8;--muted:#aab6a6;--line:#2d3a34;--accent:#f0a35c;--accent2:#76c7b7;--ok:#76d39a;--warn:#f5c46b;--bad:#ff8278;--panel:rgba(25,34,30,.88);--shadow:0 24px 80px rgba(0,0,0,.34);--glow:rgba(118,199,183,.14)}
*{box-sizing:border-box}body{margin:0;min-height:100vh;color:var(--ink);font-family:"Avenir Next","PingFang SC","Microsoft YaHei",sans-serif;background:radial-gradient(920px 620px at -12% -20%,var(--glow),transparent 62%),radial-gradient(780px 520px at 110% 4%,rgba(47,127,114,.16),transparent 58%),linear-gradient(135deg,var(--paper),#edf1df 130%)}[data-theme="dark"] body{background:radial-gradient(920px 620px at -12% -20%,rgba(240,163,92,.14),transparent 62%),radial-gradient(780px 520px at 110% 4%,rgba(118,199,183,.16),transparent 58%),linear-gradient(135deg,#101816,#18211d 130%)}
.wrap{max-width:1240px;margin:0 auto;padding:26px 16px 42px}.hero{display:grid;grid-template-columns:1.25fr .75fr;gap:16px;align-items:stretch}.panel{background:var(--panel);border:1px solid var(--line);border-radius:26px;box-shadow:var(--shadow);backdrop-filter:blur(12px)}.headline{padding:28px;position:relative;overflow:hidden}.headline:after{content:"";position:absolute;right:-80px;top:-90px;width:260px;height:260px;border-radius:50%;background:linear-gradient(135deg,var(--accent),transparent);opacity:.16}.eyebrow{color:var(--accent2);font-weight:900;letter-spacing:.16em;font-size:12px;text-transform:uppercase}.title{font-family:"Georgia","Noto Serif SC",serif;font-size:46px;line-height:1.02;margin:10px 0 12px;letter-spacing:-.04em}.sub{color:var(--muted);line-height:1.75;max-width:760px;margin:0}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}.btn{border:1px solid var(--line);background:var(--ink);color:var(--paper);text-decoration:none;border-radius:999px;padding:10px 14px;font-weight:900;cursor:pointer}.btn.secondary{background:transparent;color:var(--ink)}.btn:hover{transform:translateY(-1px)}.clock{padding:22px;display:flex;flex-direction:column;justify-content:space-between}.time{font-size:34px;font-weight:900;letter-spacing:-.03em}.date{color:var(--muted);margin-top:6px}.statusline{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:6px 10px;font-size:12px;font-weight:900;background:rgba(255,255,255,.28)}.pill.ok{color:var(--ok);border-color:rgba(23,131,79,.36)}.pill.warn{color:var(--warn);border-color:rgba(183,106,18,.36)}.pill.bad{color:var(--bad);border-color:rgba(196,61,50,.36)}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;margin-top:14px}.card{grid-column:span 4;padding:18px;position:relative;overflow:hidden}.card.wide{grid-column:span 8}.card.full{grid-column:1/-1}.card h2{font-size:18px;margin:0 0 12px}.metric{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.metric>div{border:1px solid var(--line);border-radius:18px;padding:12px;background:rgba(255,255,255,.24)}.k{font-size:12px;color:var(--muted);margin-bottom:5px}.v{font-size:24px;font-weight:950;letter-spacing:-.03em}.list{display:grid;gap:10px}.item{border:1px solid var(--line);border-radius:17px;padding:12px;background:rgba(255,255,255,.20)}.row{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.name{font-weight:950}.muted{color:var(--muted)}.mini{font-size:12px}.danger{color:var(--bad)}.good{color:var(--ok)}.warnText{color:var(--warn)}.table{width:100%;border-collapse:collapse}.table th,.table td{border-bottom:1px solid var(--line);padding:9px 7px;text-align:left;vertical-align:top}.table th{color:var(--muted);font-size:12px}.table tr:hover{background:rgba(194,106,46,.08)}code,.pre{display:block;white-space:pre-wrap;overflow:auto;border:1px solid var(--line);border-radius:14px;padding:10px;background:rgba(0,0,0,.05);color:var(--ink)}.quick{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.quick a{display:block;border:1px solid var(--line);border-radius:18px;padding:13px;text-decoration:none;color:var(--ink);font-weight:900;background:rgba(255,255,255,.20)}.quick span{display:block;color:var(--muted);font-size:12px;font-weight:700;margin-top:4px}.briefGrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.briefBox{border:1px solid var(--line);border-radius:18px;padding:13px;background:rgba(255,255,255,.22)}.briefTitle{font-size:13px;color:var(--muted);font-weight:800}.briefMain{font-size:22px;font-weight:950;margin:5px 0}.briefNote{font-size:12px;color:var(--muted);line-height:1.5}.digestCols{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}.timeline{display:grid;gap:8px}.timeitem{display:grid;grid-template-columns:92px 1fr auto;gap:10px;align-items:center;border:1px solid var(--line);border-radius:14px;padding:9px;background:rgba(255,255,255,.18)}.linkline a{color:var(--accent2);font-weight:900;text-decoration:none}.linkline a:hover{text-decoration:underline}.fade{animation:rise .42s ease both}@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}@media(max-width:900px){.hero{grid-template-columns:1fr}.title{font-size:36px}.card,.card.wide{grid-column:1/-1}.metric,.briefGrid,.digestCols{grid-template-columns:1fr}.time{font-size:28px}.timeitem{grid-template-columns:1fr}}@media(max-width:620px){.wrap{padding:16px 10px 30px}.headline,.clock,.card{padding:16px}.title{font-size:31px}.actions{display:grid}.btn{text-align:center}.table{font-size:12px}}
</style>
</head>
<body>
<div class="wrap">
  <section class="hero">
    <div class="panel headline fade">
      <div class="eyebrow">Nanobot &#x4e2a;&#x4eba;&#x4e2d;&#x67a2;</div>
      <h1 class="title">&#x4eca;&#x65e5;&#x9a7e;&#x9a76;&#x8231;</h1>
      <p class="sub">&#x628a;&#x6587;&#x7ae0;&#x3001;LOF&#x3001;&#x5b9a;&#x65f6;&#x4efb;&#x52a1;&#x548c;&#x670d;&#x52a1;&#x5668;&#x72b6;&#x6001;&#x538b;&#x6210;&#x4e00;&#x773c;&#x80fd;&#x770b;&#x61c2;&#x7684;&#x6458;&#x8981;&#x3002;&#x4f4e;&#x4ef7;&#x503c;&#x4fe1;&#x606f;&#x8fdb;&#x770b;&#x677f;&#xff0c;&#x9ad8;&#x4ef7;&#x503c;&#x5f02;&#x5e38;&#x518d;&#x6253;&#x6270;&#x4f60;&#x3002;</p>
      <div class="actions">
        <a class="btn" href="/lof">&#x6253;&#x5f00; LOF &#x770b;&#x677f;</a>
        <a class="btn secondary" href="/rss/">RSS &#x8ba2;&#x9605;</a>
        <a class="btn secondary" href="/sidecars">&#x670d;&#x52a1;&#x603b;&#x63a7;</a>
        <button class="btn secondary" onclick="loadAll(true)">&#x5237;&#x65b0;</button>
        <button class="btn secondary" onclick="toggleTheme()">&#x660e;&#x6697;</button>
      </div>
    </div>
    <div class="panel clock fade" style="animation-delay:.06s">
      <div><div class="k">Asia/Shanghai</div><div class="time" id="clock">--:--</div><div class="date" id="date">&#x52a0;&#x8f7d;&#x4e2d;...</div></div>
      <div class="statusline" id="statusline"><span class="pill warn">&#x6b63;&#x5728;&#x8bfb;&#x53d6;&#x72b6;&#x6001;</span></div>
    </div>
  </section>
  <section class="grid">
    <article class="panel card fade" style="animation-delay:.08s"><h2>&#x7cfb;&#x7edf;&#x4f53;&#x611f;</h2><div class="metric" id="systemMetrics"></div></article>
    <article class="panel card fade" style="animation-delay:.10s"><h2>&#x670d;&#x52a1;&#x5065;&#x5eb7;</h2><div class="metric" id="sidecarMetrics"></div></article>
    <article class="panel card fade" style="animation-delay:.12s"><h2>&#x5b9a;&#x65f6;&#x4efb;&#x52a1;</h2><div class="metric" id="notifyMetrics"></div></article>
    <article class="panel card full fade" style="animation-delay:.13s"><h2>&#x4eca;&#x65e5;&#x6458;&#x8981;</h2><div id="todayBrief"></div></article>
    <article class="panel card wide fade" style="animation-delay:.14s"><h2>&#x9700;&#x8981;&#x4f60;&#x770b;</h2><div class="list" id="attention"></div></article>
    <article class="panel card fade" style="animation-delay:.16s"><h2>&#x5feb;&#x901f;&#x5165;&#x53e3;</h2><div class="quick"><a href="/lof">LOF &#x96f7;&#x8fbe;<span>&#x4f30;&#x503c; / &#x6ea2;&#x4ef7; / &#x62a5;&#x544a;</span></a><a href="/rss/">RSS &#x6587;&#x7ae0;<span>&#x5fae;&#x4fe1; / &#x9e2d;&#x54e5; / Markdown</span></a><a href="/reflexio/">Reflexio<span>&#x8bb0;&#x5fc6;&#x4e0e;&#x53cd;&#x601d;</span></a><a href="/trends/">热点雷达<span>全网热榜 / MCP 工具 / 话题分析</span></a><a href="/sidecars">&#x670d;&#x52a1;&#x603b;&#x63a7;<span>&#x65e5;&#x5fd7; / &#x91cd;&#x542f;&#x547d;&#x4ee4;</span></a></div></article>
    <article class="panel card wide fade" style="animation-delay:.18s"><h2>&#x6295;&#x8d44;&#x96f7;&#x8fbe;</h2><div id="lofRadar"></div></article>
    <article class="panel card fade" style="animation-delay:.20s"><h2>&#x4fe1;&#x606f;&#x96f7;&#x8fbe;</h2><div class="list" id="infoRadar"></div></article>
    <article class="panel card full fade" style="animation-delay:.21s"><h2>7 &#x5929;&#x5386;&#x53f2;</h2><div id="historyPanel"></div></article>

    <article class="panel card full fade" style="animation-delay:.215s"><h2>Nanobot 能力矩阵</h2><div class="quick">
      <a href="#" onclick="return false">知识收件箱<span>QQ：收一下 + 链接 / 这个值得看吗 + 链接；按需抓取 Markdown，不常驻</span></a>
      <a href="#" onclick="return false">决策助手<span>QQ：今天先看什么 / 今天怎么安排；聚合系统、文章、LOF、任务数据</span></a>
      <a href="/rss/">RSS 文章能力<span>微信、鸭哥、Markdown 预览、广告过滤，仍走 RSS sidecar</span></a>
      <a href="/trends/">热点雷达能力<span>全网热榜、搜索、话题趋势、MCP 风格工具接口，走 Trend sidecar</span></a>
      <a href="/sidecars">服务运维能力<span>内存怎么样 / 服务状态 / cron 任务怎么样；真实数据查询</span></a>
    </div><div class="muted mini" style="margin-top:10px">说明：这些是 Nanobot skill/按需脚本能力，没有独立端口和 health check，所以不会计入下面的 sidecar 服务健康数量。</div></article>
    <article class="panel card full fade" style="animation-delay:.22s"><h2>&#x670d;&#x52a1;&#x77e9;&#x9635;</h2><div style="overflow:auto"><table class="table" id="services"></table></div></article>
  </section>
</div>
<script>
const root=document.documentElement;if(localStorage.dashboardTheme==='dark')root.setAttribute('data-theme','dark');
const state={system:null,sidecars:null,lof:null,notify:null,rss:null,rssSubs:null,history:null};
function toggleTheme(){const dark=root.getAttribute('data-theme')==='dark';root.setAttribute('data-theme',dark?'light':'dark');localStorage.dashboardTheme=dark?'light':'dark'}
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function pill(cls,text){return `<span class="pill ${cls}">${esc(text)}</span>`}
function fmtPct(v){return v==null?'-':Number(v).toFixed(2)+'%'}
function fmtTime(s){if(!s)return '-';try{return new Date(s).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})}catch{return s}}
function metric(k,v,cls=''){return `<div><div class="k">${esc(k)}</div><div class="v ${cls}">${esc(v)}</div></div>`}
function serviceName(x){const m={nanobot:'\u004e\u0061\u006e\u006f\u0062\u006f\u0074 \u6838\u5fc3',rss:'\u0052\u0053\u0053 \u8ba2\u9605\u770b\u677f',qq:'\u0051\u0051 \u901a\u77e5\u6865',lof:'\u004c\u004f\u0046 \u770b\u677f',notify:'\u5b9a\u65f6\u4efb\u52a1\u6865',reflexio:'\u0052\u0065\u0066\u006c\u0065\u0078\u0069\u006f \u8bb0\u5fc6\u770b\u677f',obp:'\u004f\u0042\u0050 \u515c\u5e95\u6865','podman-public-rule':'\u516c\u7f51\u7aef\u53e3\u5b88\u536b'};return m[x?.id]||x?.name||'-'}
function jobName(j){const m={'yage-ai':'\u9e2d\u54e5 \u0041\u0049 \u8981\u95fb','wechat-sub-1':'\u5fae\u4fe1\u6587\u7ae0\u63a8\u9001\uff1a\u8bb0\u5fc6\u627f\u8f7d','wechat-sub-2':'\u5fae\u4fe1\u6587\u7ae0\u63a8\u9001\uff1a\u8bb0\u5fc6\u627f\u8f7d3','lof-morning':'\u004c\u004f\u0046 \u65e9\u5e02\u62a5\u544a','lof-noon':'\u004c\u004f\u0046 \u5348\u5e02\u62a5\u544a','lof-close':'\u004c\u004f\u0046 \u6536\u76d8\u62a5\u544a','hermes-heartbeat':'\u0048\u0045\u0052\u004d\u0045\u0053 \u5fc3\u8df3\u81ea\u68c0','weather-sz-workday':'\u6df1\u5733\u5de5\u4f5c\u65e5\u5929\u6c14','weather-gz-friday-noon':'\u5e7f\u5dde\u5468\u4e94\u5929\u6c14','weather-gz-weekend':'\u5e7f\u5dde\u5468\u672b\u5929\u6c14','weather-sz-monday':'\u6df1\u5733\u5468\u4e00\u5929\u6c14'};return m[j?.id]||j?.name||j?.id||'-'}
function statusText(s){const m={silent:'\u9759\u9ed8',sent:'\u5df2\u53d1\u9001',error:'\u9519\u8bef',running:'\u8fd0\u884c\u4e2d',timeout:'\u8d85\u65f6',ok:'\u6b63\u5e38'};return m[s]||s||'-'}
function updateClock(){const now=new Date();document.getElementById('clock').textContent=now.toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});document.getElementById('date').textContent=now.toLocaleDateString('zh-CN',{weekday:'long',year:'numeric',month:'2-digit',day:'2-digit',timeZone:'Asia/Shanghai'})}
async function getJson(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw new Error(url+' '+r.status);return r.json()}
async function loadAll(manual=false){const jobs=[['system','/api/system'],['sidecars','/api/sidecars'],['lof','/api/status'],['notify','/api/notify-jobs'],['rss','/rss/api/entries?days=1&limit=8'],['rssSubs','/rss/api/subscriptions'],['history','/api/dashboard-history']];await Promise.all(jobs.map(async ([key,url])=>{try{state[key]=await getJson(url)}catch(e){state[key]={ok:false,error:e.message}}}));renderAll(manual)}
function renderAll(manual){renderStatusline(manual);renderSystem();renderSidecars();renderNotify();renderToday();renderAttention();renderLof();renderInfo();renderHistory();renderServices()}
function renderStatusline(manual){const s=state.sidecars?.summary||{};const bad=s.unhealthy||0;const jobs=state.notify?.job_details||[];const jobErr=jobs.filter(j=>j.status?.last_status==='error').length;const lof=state.lof?.last_run?.status;document.getElementById('statusline').innerHTML=[bad?pill('bad',`\u670d\u52a1\u5f02\u5e38 ${bad}`):pill('ok',`\u670d\u52a1 ${s.healthy||0}/${s.total||0}`),jobErr?pill('bad',`\u4efb\u52a1\u9519\u8bef ${jobErr}`):pill('ok','\u4efb\u52a1\u6b63\u5e38'),lof==='ok'?pill('ok','\u004c\u004f\u0046 \u5df2\u5237\u65b0'):pill('warn','\u004c\u004f\u0046 '+(statusText(lof)||'\u672a\u77e5')),manual?pill('warn','\u5df2\u5237\u65b0'):'' ].join('')}
function renderSystem(){const m=state.system?.memory||{};const l=state.system?.loadavg||{};const d=state.system?.disk_root||{};document.getElementById('systemMetrics').innerHTML=metric('\u5185\u5b58',`${m.used_mb??'-'} / ${m.total_mb??'-'} MB`,(m.used_pct||0)>75?'warnText':'good')+metric('\u8d1f\u8f7d',`${l.one??'-'} / ${l.five??'-'}`)+metric('\u78c1\u76d8',`${d.used_pct??'-'}`)}
function renderSidecars(){const s=state.sidecars?.summary||{};document.getElementById('sidecarMetrics').innerHTML=metric('\u603b\u6570',s.total??'-')+metric('\u6b63\u5e38',s.healthy??'-','good')+metric('\u5f02\u5e38',s.unhealthy??'-',(s.unhealthy||0)?'danger':'good')}
function renderNotify(){const jobs=state.notify?.job_details||[];const enabled=jobs.filter(j=>j.enabled).length;const err=jobs.filter(j=>j.status?.last_status==='error').length;const sent=jobs.filter(j=>j.status?.last_sent).length;document.getElementById('notifyMetrics').innerHTML=metric('\u542f\u7528',enabled)+metric('\u9519\u8bef',err,err?'danger':'good')+metric('\u6700\u8fd1\u53d1\u9001',sent)}

function todayKey(){return new Date().toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'})}
function dateKey(s){if(!s)return '';try{return new Date(String(s).replace(' +08:00','+08:00')).toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'})}catch{return String(s).slice(0,10)}}
function isToday(s){return dateKey(s)===todayKey()}
function hhmm(s){if(!s)return '-';try{return new Date(String(s).replace(' +08:00','+08:00')).toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit',timeZone:'Asia/Shanghai'})}catch{return String(s).slice(11,16)||'-'}}
function sourceName(e){return e?.subscription_name||e?.source||'RSS'}
function todayJobs(){return (state.notify?.job_details||[]).filter(j=>isToday(j.status?.last_finished_at||j.status?.last_started_at))}
function todaySentJobs(){return todayJobs().filter(j=>j.status?.last_sent)}
function jobBadge(j){const st=j.status?.last_status;return pill(st==='error'?'bad':(j.status?.last_sent?'ok':'warn'),statusText(st))}
function renderToday(){
  const box=document.getElementById('todayBrief'); if(!box)return;
  const jobs=state.notify?.job_details||[];
  const todays=todayJobs();
  const sent=todaySentJobs();
  const errors=jobs.filter(j=>j.status?.last_status==='error');
  const rssItems=(state.rss?.items||[]).slice(0,8);
  const rssSubs=(state.rssSubs?.items||[]);
  const rssOk=rssSubs.filter(x=>(x.last_status||'').toLowerCase()==='ok').length;
  const side=state.sidecars?.summary||{};
  const mem=state.system?.memory||{};
  const disk=state.system?.disk_root||{};
  const lr=state.lof?.last_run||{};
  const rows=state.lof?.last_board?.rows||[];
  const high=rows.filter(r=>(r.rt_premium_pct||0)>=5);
  const lofSent=jobs.filter(j=>String(j.id||'').startsWith('lof-')&&isToday(j.status?.last_finished_at)&&j.status?.last_sent).length;
  const brief=`<div class="briefGrid">
    <div class="briefBox"><div class="briefTitle">\u4fe1\u606f</div><div class="briefMain">${rssItems.length}\u7bc7</div><div class="briefNote">RSS \u8ba2\u9605 ${rssOk}/${rssSubs.length||0} \u6b63\u5e38\uff0c\u9e2d\u54e5/\u5fae\u4fe1\u4efb\u52a1\u5df2\u7eb3\u5165\u76d1\u63a7\u3002</div></div>
    <div class="briefBox"><div class="briefTitle">\u6295\u8d44</div><div class="briefMain">${high.length}\u53ea</div><div class="briefNote">\u5b9e\u65f6\u6ea2\u4ef7\u22655%\uff1bLOF \u4eca\u65e5\u63a8\u9001 ${lofSent}/3\uff0c\u6700\u65b0\u72b6\u6001 ${statusText(lr.status)}\u3002</div></div>
    <div class="briefBox"><div class="briefTitle">\u4efb\u52a1</div><div class="briefMain">${todays.length}\u6b21</div><div class="briefNote">\u4eca\u65e5\u5df2\u5b8c\u6210\u89e6\u53d1\uff1b\u53d1\u9001 ${sent.length}\u6b21\uff0c\u9519\u8bef ${errors.length}\u4e2a\u3002</div></div>
    <div class="briefBox"><div class="briefTitle">\u7cfb\u7edf</div><div class="briefMain">${mem.used_mb??'-'}MB</div><div class="briefNote">\u670d\u52a1 ${side.healthy||0}/${side.total||0} \u6b63\u5e38\uff0c\u78c1\u76d8 ${disk.used_pct??'-'}\uff0c\u5185\u5b58\u5360\u7528 ${mem.used_pct??'-'}%\u3002</div></div>
  </div>`;
  const focus=[];
  errors.slice(0,2).forEach(j=>focus.push(`<div class="item"><div class="name danger">\u4efb\u52a1\u5931\u8d25\uff1a${esc(jobName(j))}</div><div class="muted mini">${esc(j.status?.last_error||j.id)}</div></div>`));
  rssItems.slice(0,3).forEach(e=>focus.push(`<div class="item linkline"><div class="name"><a href="${esc(e.link||'/rss/')}" target="_blank" rel="noopener">${esc(e.title||'\u672a\u547d\u540d\u6587\u7ae0')}</a></div><div class="muted mini">${esc(sourceName(e))} \u00b7 ${esc(e.published_at_local||e.published_at||e.inserted_at||'-')}</div></div>`));
  high.slice(0,2).forEach(r=>focus.push(`<div class="item"><div class="name warnText">${esc(r.code)} ${esc(r.name)} \u9ad8\u6ea2\u4ef7</div><div class="muted mini">\u5b9e\u65f6 ${fmtPct(r.rt_premium_pct)} / \u6700\u65b0 ${fmtPct(r.latest_premium_pct)} / ${esc(r.limit_text||'-')}</div></div>`));
  if(!focus.length)focus.push(`<div class="item"><div class="name good">\u4eca\u5929\u6ca1\u6709\u7ea2\u8272\u4e8b\u9879</div><div class="muted mini">\u4fe1\u606f\u3001\u6295\u8d44\u548c\u7cfb\u7edf\u90fd\u6ca1\u6709\u9700\u8981\u7acb\u523b\u5904\u7406\u7684\u544a\u8b66\u3002</div></div>`);
  const timeline=todays.slice().sort((a,b)=>String(b.status?.last_finished_at||'').localeCompare(String(a.status?.last_finished_at||''))).slice(0,8);
  const timelineHtml=timeline.length?timeline.map(j=>`<div class="timeitem"><div class="muted mini">${hhmm(j.status?.last_finished_at||j.status?.last_started_at)}</div><div><div class="name">${esc(jobName(j))}</div><div class="muted mini">${esc(j.schedule_note||j.schedule||'')}</div></div><div>${jobBadge(j)}</div></div>`).join(''):`<div class="muted">\u4eca\u5929\u8fd8\u6ca1\u6709\u4efb\u52a1\u5b8c\u6210\u8bb0\u5f55\u3002</div>`;
  box.innerHTML=brief+`<div class="digestCols"><div><h2 style="font-size:16px;margin:0 0 10px">\u4eca\u65e5\u91cd\u70b9</h2><div class="list">${focus.slice(0,6).join('')}</div></div><div><h2 style="font-size:16px;margin:0 0 10px">\u4eca\u65e5\u65f6\u95f4\u7ebf</h2><div class="timeline">${timelineHtml}</div></div></div>`;
}
function renderAttention(){const box=document.getElementById('attention');const items=[];(state.sidecars?.items||[]).filter(x=>!x.ok).forEach(x=>items.push({level:'bad',title:`${serviceName(x)} \u5f02\u5e38`,body:x.error||x.check_status||'\u5065\u5eb7\u68c0\u67e5\u5931\u8d25'}));(state.notify?.job_details||[]).filter(j=>j.status?.last_status==='error').forEach(j=>items.push({level:'bad',title:`\u4efb\u52a1\u5931\u8d25\uff1a${jobName(j)}`,body:j.status?.last_error||j.id}));const rows=state.lof?.last_board?.rows||[];rows.filter(r=>(r.rt_premium_pct||0)>=5).slice(0,3).forEach(r=>items.push({level:'warn',title:`\u9ad8\u6ea2\u4ef7\uff1a${r.code} ${r.name}`,body:`\u5b9e\u65f6 ${fmtPct(r.rt_premium_pct)} / \u6700\u65b0 ${fmtPct(r.latest_premium_pct)} / ${r.limit_text||'-'}`}));if(!items.length)items.push({level:'ok',title:'\u6ca1\u6709\u9700\u8981\u7acb\u523b\u5904\u7406\u7684\u544a\u8b66',body:'\u670d\u52a1\u3001\u4efb\u52a1\u548c \u004c\u004f\u0046 \u96f7\u8fbe\u76ee\u524d\u7a33\u5b9a\u3002'});box.innerHTML=items.slice(0,6).map(x=>`<div class="item"><div class="row"><div><div class="name ${x.level==='bad'?'danger':x.level==='warn'?'warnText':'good'}">${esc(x.title)}</div><div class="muted mini">${esc(x.body)}</div></div>${pill(x.level==='bad'?'bad':x.level==='warn'?'warn':'ok',x.level==='bad'?'\u5904\u7406':x.level==='warn'?'\u5173\u6ce8':'\u6b63\u5e38')}</div></div>`).join('')}
function renderLof(){const lr=state.lof?.last_run||{};const rows=[...(state.lof?.last_board?.rows||[])].sort((a,b)=>(b.rt_premium_pct??-999)-(a.rt_premium_pct??-999)).slice(0,6);const table=`<table class="table"><thead><tr><th>\u4ee3\u7801</th><th>\u540d\u79f0</th><th>\u5b9e\u65f6\u6ea2\u4ef7</th><th>\u6700\u65b0\u6ea2\u4ef7</th><th>\u9650\u989d</th></tr></thead><tbody>${rows.map(r=>`<tr><td><a href="https://fund.eastmoney.com/${esc(r.code)}.html" target="_blank">${esc(r.code)}</a></td><td>${esc(r.name)}</td><td class="${(r.rt_premium_pct||0)>=5?'warnText':'good'}">${fmtPct(r.rt_premium_pct)}</td><td>${fmtPct(r.latest_premium_pct)}</td><td>${esc(r.limit_text||'-')}</td></tr>`).join('')}</tbody></table>`;const report=(lr.report||'').split('\n').slice(0,8).join('\n');document.getElementById('lofRadar').innerHTML=`<div class="row"><div><div class="name">${esc(lr.tag||'LOF')}</div><div class="muted mini">\u5b8c\u6210\uff1a${fmtTime(lr.finished_at)} \u00b7 ${lr.duration_ms??'-'}ms \u00b7 ${statusText(lr.status)}</div></div><a class="btn secondary" href="/lof">\u8be6\u60c5</a></div><div style="margin-top:12px;overflow:auto">${table}</div><details style="margin-top:10px"><summary class="muted">\u62a5\u544a\u6458\u8981</summary><code>${esc(report||lr.error||'\u6682\u65e0')}</code></details>`}
function renderInfo(){const jobs=state.notify?.job_details||[];const ids=['yage-ai','wechat-sub-1','wechat-sub-2','hermes-heartbeat'];document.getElementById('infoRadar').innerHTML=ids.map(id=>jobs.find(j=>j.id===id)).filter(Boolean).map(j=>`<div class="item"><div class="row"><div><div class="name">${esc(jobName(j))}</div><div class="muted mini">\u4e0b\u6b21\uff1a${esc((j.next_runs||[])[0]||'-')} \u00b7 \u6700\u8fd1\uff1a${esc(j.status?.last_finished_at||'-')}</div></div>${pill(j.status?.last_status==='error'?'bad':(j.status?.last_sent?'ok':'warn'),statusText(j.status?.last_status))}</div></div>`).join('')||'<div class="muted">\u6682\u65e0\u4efb\u52a1\u6570\u636e</div>'}
function renderHistory(){const box=document.getElementById('historyPanel');if(!box)return;const items=state.history?.items||[];if(!items.length){box.innerHTML='<div class="muted">\u5386\u53f2\u4ece\u73b0\u5728\u5f00\u59cb\u8bb0\u5f55\uff0c\u6682\u65e0\u6837\u672c\u3002</div>';return}const rows=[...items].reverse();box.innerHTML=`<div style="overflow:auto"><table class="table"><thead><tr><th>\u65e5\u671f</th><th>\u5185\u5b58\u5cf0\u503c</th><th>\u670d\u52a1</th><th>\u4efb\u52a1</th><th>\u6587\u7ae0</th><th>LOF</th><th>\u66f4\u65b0</th></tr></thead><tbody>${rows.map(x=>`<tr><td><b>${esc(x.day)}</b></td><td>${esc(x.memory_used_max_mb??x.memory_used_mb??'-')} MB<br><span class="muted mini">\u5f53\u524d ${esc(x.memory_used_mb??'-')} MB / ${esc(x.memory_used_pct??'-')}%</span></td><td>${pill((x.service_unhealthy||0)>0?'bad':'ok',`${x.service_healthy||0}/${x.service_total||0}`)}<br><span class="muted mini">\u5f02\u5e38\u5cf0\u503c ${esc(x.service_unhealthy_max??0)}</span></td><td>${esc(x.task_runs??0)} \u6b21 / \u53d1\u9001 ${esc(x.task_sent??0)}<br><span class="${(x.task_errors_max||0)>0?'danger':'good'} mini">\u9519\u8bef\u5cf0\u503c ${esc(x.task_errors_max??0)}</span></td><td>${esc(x.articles??0)} \u7bc7</td><td>${esc(x.lof_high_premium??0)} \u53ea<br><span class="muted mini">\u5cf0\u503c ${esc(x.lof_high_premium_max??0)}</span></td><td class="mini">${esc(x.updated_at||'-')}</td></tr>`).join('')}</tbody></table></div><div class="muted mini" style="margin-top:8px">${esc(state.history?.note||'\u6bcf\u6b21\u6253\u5f00\u6216\u5237\u65b0\u9a7e\u9a76\u8231\u65f6\u8bb0\u5f55\u4e00\u4efd\u5f53\u65e5\u5feb\u7167\uff0c\u4fdd\u7559\u6700\u8fd1 7 \u5929\u3002')}</div>`}
function accessUrl(x){if(!x.homepage_url)return '\u5185\u90e8';try{return new URL(x.homepage_url,location.origin).pathname}catch{return x.homepage_url}}
function renderServices(){const rows=state.sidecars?.items||[];document.getElementById('services').innerHTML=`<thead><tr><th>\u670d\u52a1</th><th>\u72b6\u6001</th><th>\u5165\u53e3</th><th>\u76d1\u542c</th><th>\u5ef6\u8fdf</th><th>\u6700\u8fd1\u544a\u8b66</th></tr></thead><tbody>${rows.map(x=>`<tr><td><b>${esc(serviceName(x))}</b><br><span class="muted mini">${esc(x.id)}</span></td><td>${pill(x.ok?'ok':'bad',x.ok?'\u6b63\u5e38':(x.check_status||'-'))}</td><td>${x.homepage_url?`<a href="${esc(x.homepage_url)}">${esc(accessUrl(x))}</a>`:'\u5185\u90e8'}</td><td>${x.port?(x.public?'0.0.0.0':'127.0.0.1')+':'+x.port:'-'}</td><td>${x.latency_ms??'-'} ms</td><td class="mini">${esc((x.recent_errors||[])[0]||'-')}</td></tr>`).join('')}</tbody>`}
updateClock();setInterval(updateClock,1000);loadAll();setInterval(()=>loadAll(false),60000);
</script>
</body>
</html>"##.to_string(),
    )
}

async fn sidecars_page() -> impl IntoResponse {
    Html(
        r##"<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Nanobot Sidecar &#x603b;&#x63a7;&#x53f0;</title>
<style>
:root{--bg:#eef3ea;--panel:#fffdf7;--text:#20231d;--muted:#68705f;--line:#d7decf;--ok:#18864b;--bad:#c13c2f;--warn:#b7791f;--accent:#2f6f88;--shadow:0 18px 45px rgba(35,48,32,.12)}
[data-theme="dark"]{--bg:#141a17;--panel:#202821;--text:#edf5ea;--muted:#a9b6a5;--line:#354035;--ok:#68d391;--bad:#fc8181;--warn:#f6c177;--accent:#7dd3fc;--shadow:0 18px 45px rgba(0,0,0,.28)}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(900px 500px at 0 -10%,rgba(102,153,102,.28),transparent 55%),radial-gradient(720px 420px at 100% 0,rgba(47,111,136,.20),transparent 50%),var(--bg);color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft Yahei",sans-serif}.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 34px}.hero{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:16px}.title{margin:0;font-size:30px;letter-spacing:-.03em}.sub{margin:8px 0 0;color:var(--muted);line-height:1.6}.toolbar{display:flex;gap:10px;flex-wrap:wrap}button,a.btn{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:12px;padding:10px 13px;box-shadow:var(--shadow);text-decoration:none;font-weight:700;cursor:pointer}.copybtn{box-shadow:none;padding:6px 9px;border-radius:9px;font-size:12px}.cmdtop{display:flex;justify-content:space-between;align-items:center;gap:8px;color:var(--muted);font-size:13px}.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:16px 0}.stat{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:16px;box-shadow:var(--shadow)}.stat b{display:block;font-size:28px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:16px;box-shadow:var(--shadow);position:relative;overflow:hidden}.card:before{content:"";position:absolute;inset:0 0 auto;height:4px;background:var(--accent)}.card.ok:before{background:var(--ok)}.card.bad:before{background:var(--bad)}.row{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.name{font-size:18px;font-weight:800}.desc{color:var(--muted);margin:7px 0 12px;line-height:1.55}.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:5px 9px;font-size:12px;font-weight:800;border:1px solid var(--line);white-space:nowrap}.pill.ok{color:var(--ok);background:rgba(24,134,75,.08);border-color:rgba(24,134,75,.3)}.pill.bad{color:var(--bad);background:rgba(193,60,47,.08);border-color:rgba(193,60,47,.3)}.pill.warn{color:var(--warn)}.meta{display:grid;grid-template-columns:90px 1fr;gap:6px 8px;color:var(--muted);font-size:13px}.meta b{color:var(--text);font-weight:700;overflow-wrap:anywhere}.cmd{margin-top:12px;display:grid;gap:7px}code{display:block;white-space:pre-wrap;overflow:auto;background:rgba(90,100,80,.12);border:1px solid var(--line);border-radius:10px;padding:8px;color:var(--text);user-select:text}.links{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.links a{color:var(--accent);font-weight:800;text-decoration:none}.links a:hover{text-decoration:underline}.foot{margin-top:16px;color:var(--muted);font-size:13px}.modal{position:fixed;inset:0;background:rgba(0,0,0,.42);display:none;align-items:center;justify-content:center;padding:18px;z-index:20}.modal.show{display:flex}.dialog{width:min(940px,100%);max-height:88vh;overflow:auto;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:22px;box-shadow:0 24px 80px rgba(0,0,0,.35)}.dialogHead{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;padding:18px 18px 12px;border-bottom:1px solid var(--line)}.dialogTitle{margin:0;font-size:22px}.dialogBody{padding:16px 18px 18px}.miniTable{width:100%;border-collapse:collapse;min-width:760px}.miniTable th,.miniTable td{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}.miniTable th{color:var(--muted);font-size:12px}.pre{display:block;white-space:pre-wrap;overflow:auto;max-height:180px;background:rgba(90,100,80,.12);border:1px solid var(--line);border-radius:10px;padding:8px}.jobDetail{margin-top:8px;border:1px solid var(--line);border-radius:14px;padding:10px 12px;background:rgba(90,100,80,.08)}.jobDetail summary{cursor:pointer;color:var(--accent);font-weight:800}.jobDetail summary:hover{text-decoration:underline}.jobDetail[open]{background:rgba(90,100,80,.12)}.jobDetailBody{margin-top:10px}.miniTable td:nth-child(3){white-space:nowrap}.miniTable td:nth-child(5){white-space:nowrap}@media(max-width:720px){.hero{display:block}.toolbar{margin-top:12px}.stats,.portGrid{grid-template-columns:1fr}.portHead{display:block}.portHead button{margin-top:10px}.title{font-size:25px}}
</style>
</head>
<body>
<div class="wrap">
  <section class="hero">
    <div>
      <h1 class="title">Nanobot Sidecar &#x603b;&#x63a7;&#x53f0;</h1>
      <p class="sub">&#x4e00;&#x4e2a;&#x8f7b;&#x91cf;&#x53ea;&#x8bfb;&#x603b;&#x63a7;&#x53f0;&#xff1a;&#x67e5;&#x770b;&#x5065;&#x5eb7;&#x72b6;&#x6001;&#x3001;&#x7aef;&#x53e3;&#x3001;&#x65e5;&#x5fd7;&#x547d;&#x4ee4;&#x3001;&#x516c;&#x7f51;&#x66b4;&#x9732;&#x548c;&#x6700;&#x8fd1;&#x544a;&#x8b66;&#x3002;&#x4e0d;&#x65b0;&#x589e;&#x5e38;&#x9a7b;&#x8fdb;&#x7a0b;&#x3002;</p>
    </div>
    <div class="toolbar">
      <button onclick="loadAll()">&#x5237;&#x65b0;&#x72b6;&#x6001;</button>
      <button onclick="toggleTheme()">&#x5207;&#x6362;&#x660e;&#x6697;</button>
      <a class="btn" href="/">&#x56de;&#x5230;&#x9a7e;&#x9a76;&#x8231;</a><a class="btn" href="/lof">LOF &#x770b;&#x677f;</a>
    </div>
  </section>
  <section class="stats" id="stats"></section>

  <section class="grid" id="abilityGrid" style="margin-bottom:14px">
    <article class="card ok"><div class="row"><div><div class="name">知识收件箱</div><div class="desc">通用链接收纳、Markdown 抓取、待读列表和“值得看吗”判断。按需运行，不占常驻内存。</div></div><span class="pill ok">能力 · 按需</span></div><div class="meta"><span>触发语</span><b>收一下 + 链接 / 这个值得看吗 + 链接 / 收件箱</b><span>脚本</span><b>/root/.nanobot/workspace/skills/knowledge-inbox/inbox.py</b><span>数据</span><b>/root/.nanobot/data/knowledge-inbox</b></div></article>
    <article class="card ok"><div class="row"><div><div class="name">个人决策助手</div><div class="desc">把系统状态、文章、LOF、定时任务合成“今天先看什么 / 下一步做什么”的建议。</div></div><span class="pill ok">能力 · 只读</span></div><div class="meta"><span>触发语</span><b>今天先看什么 / 今天怎么安排 / 有什么建议</b><span>脚本</span><b>/root/.nanobot/workspace/skills/personal-ops-assistant/ops_summary.py decision</b><span>来源</span><b>8093 API + RSS + Notify + LOF</b></div></article>
    <article class="card ok"><div class="row"><div><div class="name">文章雷达</div><div class="desc">微信文章、鸭哥 AI、Markdown 预览和广告过滤。运行态由 RSS sidecar 提供。</div></div><span class="pill ok">能力 · sidecar backed</span></div><div class="meta"><span>触发语</span><b>今天文章有哪些 / 鸭哥更新了吗 / 微信文章有没有</b><span>入口</span><b>8093/rss/</b><span>服务</span><b>podman-wechat-rss-sidecar.service</b></div></article>
    <article class="card ok"><div class="row"><div><div class="name">热点雷达</div><div class="desc">自动收集微博、知乎、B站、百度、财联社、华尔街见闻、今日头条热榜，提供搜索、话题趋势和 MCP 风格工具。</div></div><span class="pill ok">能力 · MCP ready</span></div><div class="meta"><span>触发语</span><b>今天热点 / AI 热点 / 某话题有什么新闻</b><span>入口</span><b>8093/trends/</b><span>服务</span><b>trend-sidecar-rs.service</b></div></article>
    <article class="card ok"><div class="row"><div><div class="name">运维问答</div><div class="desc">内存、服务、cron、LOF 状态走真实本地数据，不再靠 LLM 猜。</div></div><span class="pill ok">能力 · 快捷查询</span></div><div class="meta"><span>触发语</span><b>内存怎么样 / 服务状态 / cron 任务怎么样 / LOF 有机会吗</b><span>脚本</span><b>/root/.nanobot/workspace/skills/personal-ops-assistant/ops_summary.py</b><span>说明</span><b>不重启服务，不改配置，默认只读</b></div></article>
  </section>
  <section class="grid" id="grid"></section>
  <div class="foot" id="foot">&#x52a0;&#x8f7d;&#x4e2d;...</div>
</div>
<div class="modal" id="notifyModal" onclick="if(event.target.id==='notifyModal')closeNotifyModal()"><div class="dialog"><div class="dialogHead"><div><h2 class="dialogTitle">Notify &#x4efb;&#x52a1;&#x8be6;&#x60c5;</h2><div class="muted" id="notifySub">Loading...</div></div><button onclick="closeNotifyModal()">&#x5173;&#x95ed;</button></div><div class="dialogBody" id="notifyBody"></div></div></div>
<script>
const root=document.documentElement;if(localStorage.sidecarTheme==='dark')root.setAttribute('data-theme','dark');
function toggleTheme(){const d=root.getAttribute('data-theme')==='dark';root.setAttribute('data-theme',d?'light':'dark');localStorage.sidecarTheme=d?'light':'dark'}
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function pill(ok,text){return `<span class="pill ${ok?'ok':'bad'}">${ok?'\u6b63\u5e38':'\u5f02\u5e38'} \u00b7 ${esc(text||'-')}</span>`}
function copyText(text,btn){
  const done=()=>{const old=btn.textContent;btn.textContent='\u5df2\u590d\u5236';setTimeout(()=>btn.textContent=old,1200)};
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(done).catch(()=>fallbackCopy(text,done));}
  else{fallbackCopy(text,done);}
}
function fallbackCopy(text,done){const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.left='-9999px';document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();done&&done();}
function cmdHtml(label,text){return `<div class="cmdtop"><span>${esc(label)}</span><button class="copybtn" onclick='copyText(${JSON.stringify(text||'')},this)'>\u590d\u5236</button></div><code>${esc(text||'-')}</code>`}
function accessText(x){
  if(x.homepage_url){try{const u=new URL(x.homepage_url, window.location.origin);return u.origin+u.pathname;}catch(e){return x.homepage_url}}
  return '\u65e0\u516c\u7f51\u5165\u53e3';
}
function listenText(x){
  if(x.port==null)return '-';
  return (x.public?'0.0.0.0':'127.0.0.1')+':'+x.port;
}
function exposureText(x){
  if(x.public)return '\u76f4\u63a5\u516c\u7f51';
  if(x.homepage_url)return '\u7ecf 8093 \u4ee3\u7406';
  return '\u4ec5\u5185\u90e8';
}
function render(d){
  const s=d.summary||{total:0,healthy:0,unhealthy:0};
  document.getElementById('stats').innerHTML=`<div class="stat"><span>\u603b\u670d\u52a1</span><b>${s.total}</b></div><div class="stat"><span>\u6b63\u5e38</span><b style="color:var(--ok)">${s.healthy}</b></div><div class="stat"><span>\u5f02\u5e38</span><b style="color:var(--bad)">${s.unhealthy}</b></div>`;
  document.getElementById('grid').innerHTML=(d.items||[]).map(x=>`<article class="card ${x.ok?'ok':'bad'}">
    <div class="row"><div><div class="name">${esc(x.name)}</div><div class="desc">${esc(x.description)}</div></div>${pill(x.ok,x.check_status)}</div>
    <div class="meta">
      <span>\u670d\u52a1 ID</span><b>${esc(x.id)}</b>
      <span>\u8bbf\u95ee\u5165\u53e3</span><b>${esc(accessText(x))}</b>
      <span>\u670d\u52a1\u76d1\u542c</span><b>${esc(listenText(x))}</b>
      <span>\u66b4\u9732\u65b9\u5f0f</span><b>${esc(exposureText(x))}</b>
      <span>\u7cfb\u7edf\u670d\u52a1</span><b>${esc(x.unit_status || (x.unit ? '\u672a\u77e5' : '\u672a\u6258\u7ba1'))}</b>
      <span>\u5ef6\u8fdf</span><b>${x.latency_ms==null?'-':x.latency_ms+' ms'}</b>
      <span>\u542f\u52a8</span><b>${esc(x.active_since||'-')}</b>
      <span>\u9519\u8bef</span><b>${esc(x.error||'-')}</b>
    </div>
    ${(x.recent_errors||[]).length?`<div class="cmd">${cmdHtml('\u6700\u8fd1\u544a\u8b66 / \u9519\u8bef',(x.recent_errors||[]).join('\\n'))}</div>`:''}
    <div class="links">${x.homepage_url?`<a href="${esc(x.homepage_url)}" target="_blank" rel="noopener">\u6253\u5f00\u9875\u9762</a>`:''}${x.id==='notify'?`<a href="#" onclick="openNotifyJobs();return false;">\u67e5\u770b\u4efb\u52a1\u8be6\u60c5</a>`:''}<a href="/api/sidecars" target="_blank">\u72b6\u6001 JSON</a></div>
    <div class="cmd">${cmdHtml('\u67e5\u770b\u65e5\u5fd7',x.logs_command)}${cmdHtml('\u91cd\u542f\u670d\u52a1',x.restart_command)}</div>
  </article>`).join('');
  document.getElementById('foot').textContent=`\u6700\u540e\u5237\u65b0\uff1a${d.now || '-'}\u3002\u9875\u9762\u53ea\u8bfb\uff0c\u4e0d\u4f1a\u5728\u7f51\u9875\u4e0a\u6267\u884c\u91cd\u542f\uff1b\u9700\u8981\u64cd\u4f5c\u65f6\u8bf7\u590d\u5236\u547d\u4ee4\u5230 SSH \u7ec8\u7aef\u6267\u884c\u3002`;
}
async function loadAll(){try{const r=await fetch('/api/sidecars',{cache:'no-store'});render(await r.json())}catch(e){document.getElementById('foot').textContent='\u52a0\u8f7d\u5931\u8d25\uff1a'+e.message}}
function notifyStatusPill(st){const s=st||'-';const cls=s==='sent'?'ok':(s==='error'?'bad':(s==='running'?'warn':''));return `<span class="pill ${cls}">${esc(s)}</span>`}
async function openNotifyJobs(){const modal=document.getElementById('notifyModal');modal.classList.add('show');document.getElementById('notifySub').textContent='\u52a0\u8f7d\u4e2d...';document.getElementById('notifyBody').innerHTML='';try{const r=await fetch('/api/notify-jobs',{cache:'no-store'});const d=await r.json();renderNotifyJobs(d)}catch(e){document.getElementById('notifySub').textContent='\u52a0\u8f7d\u5931\u8d25\uff1a'+e.message}}
function closeNotifyModal(){document.getElementById('notifyModal').classList.remove('show')}
function renderNotifyJobs(d){
  const jobs=d.job_details||[];
  document.getElementById('notifySub').textContent=`${d.now||'-'} \u00b7 ${jobs.length} \u4e2a\u4efb\u52a1 \u00b7 ${d.target_set?'QQ \u76ee\u6807\u5df2\u914d\u7f6e':'QQ \u76ee\u6807\u672a\u914d\u7f6e'}`;
  document.getElementById('notifyBody').innerHTML=`<div style="overflow:auto"><table class="miniTable"><thead><tr><th>\u4efb\u52a1</th><th>\u89c4\u5219</th><th>\u4e0b\u6b21\u8fd0\u884c</th><th>\u72b6\u6001</th><th>\u6700\u8fd1\u5b8c\u6210</th><th>\u8be6\u60c5</th></tr></thead><tbody>${jobs.map(j=>`<tr><td><b>${esc(j.name)}</b><br><span class="muted">${esc(j.id)}</span></td><td><code>${esc(j.schedule)}</code><br><span class="muted">${esc(j.schedule_note)}</span></td><td>${esc((j.next_runs||[])[0]||'-')}</td><td>${j.enabled?'<span class="pill ok">\u542f\u7528</span>':'<span class="pill">\u6682\u505c</span>'}<br>${notifyStatusPill(j.status?.last_status)}</td><td>${esc(j.status?.last_finished_at||'-')}</td><td><details class="jobDetail"><summary>\u5c55\u5f00</summary><div class="jobDetailBody"><b>\u672a\u6765\u89e6\u53d1</b><br>${(j.next_runs||[]).map(x=>`<span class="pill warn">${esc(x)}</span>`).join(' ')||'<span class="muted">-</span>'}<br><br><b>\u5b9e\u9645\u547d\u4ee4</b><button class="copybtn" style="margin-left:8px" onclick='copyText(${JSON.stringify(j.command||'')},this)'>\u590d\u5236</button><div class="pre">${esc(j.command||'-')}</div>${j.status?.last_error?`<br><b>\u6700\u8fd1\u9519\u8bef</b><div class="pre">${esc(j.status.last_error)}</div>`:''}${j.status?.last_stdout_preview?`<br><b>\u6700\u8fd1\u8f93\u51fa\u6458\u8981</b><div class="pre">${esc(j.status.last_stdout_preview)}</div>`:''}</div></details></td></tr>`).join('')}</tbody></table></div>`
}
loadAll();setInterval(loadAll,15000);
</script>
</body>
</html>"##,
    )
}

fn shanghai_now() -> DateTime<FixedOffset> {
    let sh_tz = FixedOffset::east_opt(8 * 3600).expect("tz");
    Utc::now().with_timezone(&sh_tz)
}

async fn sidecar_manager_snapshot(state: &AppState) -> SidecarManagerResponse {
    let configs = load_managed_sidecars().await;
    let mut items = Vec::new();
    for cfg in configs {
        items.push(check_managed_sidecar(&state.http, cfg).await);
    }
    let healthy = items.iter().filter(|item| item.ok).count();
    let total = items.len();
    SidecarManagerResponse {
        now: shanghai_now().format("%Y-%m-%d %H:%M:%S %:z").to_string(),
        summary: SidecarManagerSummary {
            total,
            healthy,
            unhealthy: total.saturating_sub(healthy),
        },
        items,
    }
}

async fn load_managed_sidecars() -> Vec<ManagedSidecar> {
    let path = std::env::var("SIDECAR_MANAGER_CONFIG")
        .unwrap_or_else(|_| "/root/.nanobot/sidecars.json".to_string());
    match tokio::fs::read_to_string(&path).await {
        Ok(text) => serde_json::from_str::<Vec<ManagedSidecar>>(&text)
            .unwrap_or_else(|_| default_managed_sidecars()),
        Err(_) => default_managed_sidecars(),
    }
}

fn default_managed_sidecars() -> Vec<ManagedSidecar> {
    vec![ManagedSidecar {
        id: "lof".into(),
        name: "LOF Sidecar".into(),
        description: "LOF data board and reports".into(),
        port: Some(8093),
        unit: Some("lof-sidecar.service".into()),
        homepage_url: Some("/".into()),
        check_url: Some("http://127.0.0.1:8093/health".into()),
        check_kind: Some("http".into()),
        public: true,
        logs_command: "journalctl -u lof-sidecar.service -f".into(),
        restart_command: "systemctl restart lof-sidecar.service".into(),
    }]
}

async fn check_managed_sidecar(client: &Client, cfg: ManagedSidecar) -> ManagedSidecarStatus {
    let unit_status = check_systemd_unit(cfg.unit.as_deref()).await;
    let active_since = check_systemd_active_since(cfg.unit.as_deref()).await;
    let recent_errors =
        check_systemd_recent_errors(cfg.unit.as_deref(), active_since.as_deref()).await;
    let started = Instant::now();
    let mut ok = false;
    let mut check_status = "unknown".to_string();
    let mut http_code = None;
    let mut latency_ms = None;
    let mut error = None;
    let kind = cfg.check_kind.as_deref().unwrap_or("http");

    if kind == "tcp" {
        if let Some(port) = cfg.port {
            match tokio::time::timeout(
                Duration::from_secs(2),
                TcpStream::connect(("127.0.0.1", port)),
            )
            .await
            {
                Ok(Ok(_)) => {
                    ok = true;
                    check_status = "tcp open".to_string();
                    latency_ms = Some(started.elapsed().as_millis());
                }
                Ok(Err(e)) => {
                    check_status = "tcp closed".to_string();
                    error = Some(e.to_string());
                    latency_ms = Some(started.elapsed().as_millis());
                }
                Err(_) => {
                    check_status = "tcp timeout".to_string();
                    error = Some("tcp check timed out".to_string());
                    latency_ms = Some(started.elapsed().as_millis());
                }
            }
        } else {
            error = Some("missing port for tcp check".to_string());
        }
    } else if kind == "unit" {
        ok = matches!(unit_status.as_deref(), Some("active"));
        check_status = unit_status.clone().unwrap_or_else(|| "unknown".to_string());
        latency_ms = Some(started.elapsed().as_millis());
    } else if let Some(url) = cfg.check_url.as_deref() {
        match tokio::time::timeout(Duration::from_secs(3), client.get(url).send()).await {
            Ok(Ok(resp)) => {
                let status = resp.status();
                http_code = Some(status.as_u16());
                ok = status.is_success();
                check_status = format!("http {}", status.as_u16());
                latency_ms = Some(started.elapsed().as_millis());
            }
            Ok(Err(e)) => {
                check_status = "http error".to_string();
                error = Some(e.to_string());
                latency_ms = Some(started.elapsed().as_millis());
            }
            Err(_) => {
                check_status = "http timeout".to_string();
                error = Some("http check timed out".to_string());
                latency_ms = Some(started.elapsed().as_millis());
            }
        }
    } else if let Some(port) = cfg.port {
        match tokio::time::timeout(
            Duration::from_secs(2),
            TcpStream::connect(("127.0.0.1", port)),
        )
        .await
        {
            Ok(Ok(_)) => {
                ok = true;
                check_status = "tcp open".to_string();
                latency_ms = Some(started.elapsed().as_millis());
            }
            Ok(Err(e)) => {
                check_status = "tcp closed".to_string();
                error = Some(e.to_string());
                latency_ms = Some(started.elapsed().as_millis());
            }
            Err(_) => {
                check_status = "tcp timeout".to_string();
                error = Some("tcp check timed out".to_string());
                latency_ms = Some(started.elapsed().as_millis());
            }
        }
    } else {
        ok = matches!(unit_status.as_deref(), Some("active"));
        check_status = unit_status
            .clone()
            .unwrap_or_else(|| "not configured".to_string());
    }

    if cfg.unit.as_deref().is_some_and(|u| !u.trim().is_empty())
        && !matches!(unit_status.as_deref(), Some("active"))
    {
        ok = false;
    }

    ManagedSidecarStatus {
        id: cfg.id,
        name: cfg.name,
        description: cfg.description,
        port: cfg.port,
        unit: cfg.unit,
        homepage_url: cfg.homepage_url,
        public: cfg.public,
        ok,
        check_status,
        unit_status,
        http_code,
        latency_ms,
        error,
        active_since,
        recent_errors,
        logs_command: cfg.logs_command,
        restart_command: cfg.restart_command,
    }
}

async fn check_systemd_active_since(unit: Option<&str>) -> Option<String> {
    let unit = unit?.trim();
    if unit.is_empty() {
        return None;
    }
    let output = tokio::time::timeout(
        Duration::from_secs(2),
        Command::new("systemctl")
            .arg("show")
            .arg(unit)
            .arg("-p")
            .arg("ActiveEnterTimestamp")
            .arg("--value")
            .output(),
    )
    .await;
    match output {
        Ok(Ok(out)) => {
            let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if text.is_empty() {
                None
            } else {
                Some(text)
            }
        }
        _ => None,
    }
}

fn journal_since_value(active_since: Option<&str>) -> Option<String> {
    let text = active_since?.trim();
    if text.is_empty() {
        return None;
    }
    let parts: Vec<&str> = text.split_whitespace().collect();
    if parts.len() >= 3 && parts[1].chars().take(4).all(|c| c.is_ascii_digit()) {
        let value = format!("{} {}", parts[1], parts[2]);
        chrono::NaiveDateTime::parse_from_str(&value, "%Y-%m-%d %H:%M:%S")
            .map(|dt| {
                (dt + ChronoDuration::seconds(1))
                    .format("%Y-%m-%d %H:%M:%S")
                    .to_string()
            })
            .unwrap_or(value)
            .into()
    } else {
        Some(text.to_string())
    }
}

async fn check_systemd_recent_errors(
    unit: Option<&str>,
    active_since: Option<&str>,
) -> Vec<String> {
    let Some(unit) = unit.map(str::trim).filter(|u| !u.is_empty()) else {
        return Vec::new();
    };
    let mut cmd = Command::new("journalctl");
    cmd.arg("-u")
        .arg(unit)
        .arg("-p")
        .arg("warning..alert")
        .arg("--no-pager")
        .arg("-n")
        .arg("20");
    if let Some(since) = journal_since_value(active_since) {
        cmd.arg(format!("--since={since}"));
    }
    let output = tokio::time::timeout(Duration::from_secs(2), cmd.output()).await;
    match output {
        Ok(Ok(out)) => String::from_utf8_lossy(&out.stdout)
            .lines()
            .filter(|line| {
                let text = line.trim();
                if text.is_empty()
                    || text.contains("-- No entries --")
                    || text.starts_with("-- Journal begins")
                    || text.starts_with("Hint:")
                {
                    return false;
                }
                let lower = text.to_ascii_lowercase();
                lower.contains("error")
                    || lower.contains("warn")
                    || lower.contains("failed")
                    || lower.contains("timeout")
                    || lower.contains("traceback")
                    || lower.contains("panic")
            })
            .take(3)
            .map(|line| {
                let mut text = line.trim().to_string();
                if text.chars().count() > 180 {
                    text = text.chars().take(180).collect::<String>();
                    text.push_str("...");
                }
                text
            })
            .collect(),
        _ => Vec::new(),
    }
}

async fn check_systemd_unit(unit: Option<&str>) -> Option<String> {
    let unit = unit?.trim();
    if unit.is_empty() {
        return None;
    }
    let output = tokio::time::timeout(
        Duration::from_secs(2),
        Command::new("systemctl")
            .arg("is-active")
            .arg(unit)
            .output(),
    )
    .await;
    match output {
        Ok(Ok(out)) => {
            let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if text.is_empty() {
                Some("unknown".to_string())
            } else {
                Some(text)
            }
        }
        Ok(Err(e)) => Some(format!("error: {}", e)),
        Err(_) => Some("timeout".to_string()),
    }
}

async fn api_run(State(state): State<AppState>, Json(req): Json<RunRequest>) -> impl IntoResponse {
    let tag = req.tag.unwrap_or_else(|| "收盘".to_string());
    let run = execute_run(&state, &tag).await;

    let (status_code, ok) = if run.status == "ok" {
        (StatusCode::OK, true)
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, false)
    };

    (
        status_code,
        Json(RunResponse {
            ok,
            status: run.status.clone(),
            tag,
            duration_ms: run.duration_ms,
            report: run.report,
            error: run.error,
        }),
    )
}

async fn api_trigger(
    State(state): State<AppState>,
    Json(req): Json<RunRequest>,
) -> impl IntoResponse {
    let tag = req.tag.unwrap_or_else(|| "异步刷新".to_string());
    let st = state.clone();
    let tag_bg = tag.clone();
    tokio::spawn(async move {
        let _ = execute_run(&st, &tag_bg).await;
    });
    (
        StatusCode::ACCEPTED,
        Json(TriggerResponse { queued: true, tag }),
    )
}

async fn auto_refresh_loop(state: AppState) {
    let interval_secs: i64 = std::env::var("LOF_AUTO_REFRESH_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(300);
    let mut last_run_ts: i64 = 0;
    loop {
        let now = Utc::now();
        if is_trading_session(now) {
            let now_ts = now.timestamp();
            if now_ts - last_run_ts >= interval_secs {
                let _ = execute_run(&state, "自动刷新").await;
                last_run_ts = Utc::now().timestamp();
            }
        }
        tokio::time::sleep(Duration::from_secs(15)).await;
    }
}

fn is_trading_session(now_utc: DateTime<Utc>) -> bool {
    let sh_tz = FixedOffset::east_opt(8 * 3600).expect("tz");
    let sh = now_utc.with_timezone(&sh_tz);
    let wd = sh.weekday().number_from_monday();
    if wd > 5 {
        return false;
    }
    let hm = sh.hour() * 60 + sh.minute();
    let morning = (9 * 60 + 30) <= hm && hm <= (11 * 60 + 30);
    let afternoon = (13 * 60) <= hm && hm <= (15 * 60);
    morning || afternoon
}

async fn execute_run(state: &AppState, tag: &str) -> LastRun {
    let _guard = state.run_lock.lock().await;
    let started_at = Utc::now();
    let start = Instant::now();

    let timed = tokio::time::timeout(
        Duration::from_secs(state.timeout_secs),
        run_native_report(state, tag),
    )
    .await;

    let (run, board) = match timed {
        Err(_) => (
            LastRun {
                tag: tag.to_string(),
                started_at,
                finished_at: Utc::now(),
                duration_ms: start.elapsed().as_millis(),
                status: "timeout".to_string(),
                report: String::new(),
                error: Some(format!(
                    "native run timed out after {}s",
                    state.timeout_secs
                )),
            },
            None,
        ),
        Ok(Err(e)) => (
            LastRun {
                tag: tag.to_string(),
                started_at,
                finished_at: Utc::now(),
                duration_ms: start.elapsed().as_millis(),
                status: "error".to_string(),
                report: String::new(),
                error: Some(e),
            },
            None,
        ),
        Ok(Ok((report, board))) => (
            LastRun {
                tag: tag.to_string(),
                started_at,
                finished_at: Utc::now(),
                duration_ms: start.elapsed().as_millis(),
                status: "ok".to_string(),
                report,
                error: None,
            },
            Some(board),
        ),
    };

    persist_run(state, run, board).await
}

async fn run_native_report(state: &AppState, tag: &str) -> Result<(String, BoardData), String> {
    let history_path = state.script_dir.join("premium_history.json");

    let funds = fetch_all_funds(&state.http).await;
    if funds.is_empty() {
        return Err("no fund data fetched".to_string());
    }

    let mut history = load_history(&history_path).await;
    update_history(&mut history, &funds);
    save_history(&history_path, &history).await;

    let report = generate_report(tag, &funds, &history);
    let board = build_board(&funds, &history);
    if report.trim().is_empty() {
        return Err("empty report generated".to_string());
    }
    Ok((report, board))
}

async fn fetch_all_funds(client: &Client) -> Vec<Fund> {
    let codes: Vec<String> = QDII_CODES.iter().map(|c| (*c).to_string()).collect();
    stream::iter(codes)
        .map(|code| async move { fetch_one(client, &code).await })
        .buffer_unordered(8)
        .collect::<Vec<Fund>>()
        .await
}

async fn fetch_one(client: &Client, code: &str) -> Fund {
    let url = format!("https://www.haoetf.com/qdii/{}", code);
    match client.get(url).send().await {
        Ok(resp) if resp.status().is_success() => match resp.text().await {
            Ok(body) => parse_fund_detail(&body, code).unwrap_or_else(|| fallback_fund(code)),
            Err(_) => fallback_fund(code),
        },
        _ => fallback_fund(code),
    }
}

fn parse_fund_detail(html: &str, code: &str) -> Option<Fund> {
    let doc = ScraperHtml::parse_document(html);
    let table_sel = Selector::parse("table").ok()?;
    let tr_sel = Selector::parse("tr").ok()?;
    let cell_sel = Selector::parse("th, td").ok()?;

    for table in doc.select(&table_sel) {
        let rows: Vec<Vec<String>> = table
            .select(&tr_sel)
            .map(|tr| {
                tr.select(&cell_sel)
                    .map(|c| c.text().collect::<Vec<_>>().join("").trim().to_string())
                    .collect::<Vec<String>>()
            })
            .filter(|r| !r.is_empty())
            .collect();

        if rows.len() < 2 {
            continue;
        }
        let header = &rows[0];
        let is_main_board = header.iter().any(|h| h.contains("实时估值"))
            && header.iter().any(|h| h.contains("最新估值"))
            && header.iter().any(|h| h.contains("现价"))
            && header.iter().any(|h| h.contains("成交额"));
        if !is_main_board {
            continue;
        }
        let maybe_row = rows.iter().skip(1).find(|r| {
            r.get(0)
                .map(|s| s.chars().filter(|c| c.is_ascii_digit()).collect::<String>() == code)
                .unwrap_or(false)
        });
        let Some(cols) = maybe_row else {
            continue;
        };

        let pick = |names: &[&str]| -> Option<String> {
            for name in names {
                if let Some(idx) = header.iter().position(|h| h.contains(name)) {
                    if let Some(v) = cols.get(idx) {
                        if !v.trim().is_empty() {
                            return Some(v.trim().to_string());
                        }
                    }
                }
            }
            None
        };

        let name = cols.get(1).cloned().unwrap_or_else(|| code.to_string());
        let rt_nav = pick(&["实时估值"]).and_then(|v| parse_float(&v));
        let rt_premium_pct = pick(&["实时溢价"]).and_then(|v| parse_float(&v));
        let latest_nav = pick(&["最新估值"]).and_then(|v| parse_float(&v));
        let latest_premium_pct = pick(&["最新溢价"]).and_then(|v| parse_float(&v));
        let premium = latest_premium_pct.map(|v| v / 100.0);
        let price = pick(&["现价"]).and_then(|v| parse_float(&v));
        let change_pct = pick(&["涨跌"]).and_then(|v| parse_float(&v));
        let amount =
            pick(&["成交额(万元)", "成交额"]).and_then(|v| parse_float(&v).map(|x| x * 10_000.0));

        let mut limit_text = pick(&["申购限额", "累计申购上限"]).unwrap_or_default();
        // Some pages drop optional middle columns, causing tail fields to shift.
        // In that case infer limit from the field before fee columns, but avoid "xx万份" min-unit values.
        if limit_text.is_empty() && cols.len() >= 4 {
            let tail = cols[cols.len() - 4].trim();
            let looks_like_limit = tail.contains("暂停")
                || tail.contains("不限")
                || tail.contains('元')
                || tail == "-";
            if looks_like_limit {
                limit_text = tail.to_string();
            }
        }

        let suspended = limit_text.contains("暂停");
        let limit = if suspended {
            Some(0.0)
        } else if limit_text.contains('无') || limit_text.contains("不限") {
            None
        } else {
            parse_float(&limit_text)
        };

        return Some(Fund {
            code: code.to_string(),
            name,
            premium,
            rt_nav,
            rt_premium_pct,
            latest_nav,
            latest_premium_pct,
            price,
            change_pct,
            amount,
            limit,
            suspended,
            limit_text,
        });
    }

    None
}

fn fallback_fund(code: &str) -> Fund {
    Fund {
        code: code.to_string(),
        name: code.to_string(),
        premium: None,
        rt_nav: None,
        rt_premium_pct: None,
        latest_nav: None,
        latest_premium_pct: None,
        price: None,
        change_pct: None,
        amount: None,
        limit: None,
        suspended: false,
        limit_text: String::new(),
    }
}

fn parse_float(input: &str) -> Option<f64> {
    let filtered: String = input
        .chars()
        .filter(|c| c.is_ascii_digit() || *c == '.' || *c == '-')
        .collect();
    if filtered.is_empty() {
        None
    } else {
        filtered.parse::<f64>().ok()
    }
}

type HistoryMap = HashMap<String, HashMap<String, f64>>;

async fn load_history(path: &Path) -> HistoryMap {
    match tokio::fs::read_to_string(path).await {
        Ok(content) => serde_json::from_str::<HistoryMap>(&content).unwrap_or_default(),
        Err(_) => HashMap::new(),
    }
}

async fn save_history(path: &Path, history: &HistoryMap) {
    if let Ok(content) = serde_json::to_string_pretty(history) {
        let _ = tokio::fs::write(path, content).await;
    }
}

fn update_history(history: &mut HistoryMap, funds: &[Fund]) {
    let sh_tz = FixedOffset::east_opt(8 * 3600).expect("tz");
    let today = Utc::now().with_timezone(&sh_tz).date_naive().to_string();
    let cutoff =
        (Utc::now().with_timezone(&sh_tz).date_naive() - ChronoDuration::days(30)).to_string();

    for f in funds {
        if let Some(p) = f.premium {
            history
                .entry(f.code.clone())
                .or_default()
                .insert(today.clone(), (p * 100.0 * 100.0).round() / 100.0);
        }
    }

    for (_code, dmap) in history.iter_mut() {
        dmap.retain(|k, _| k >= &cutoff);
    }
}

fn consecutive_days(history: &HistoryMap, code: &str, threshold_percent: f64, days: i64) -> i64 {
    let sh_tz = FixedOffset::east_opt(8 * 3600).expect("tz");
    let today = Utc::now().with_timezone(&sh_tz).date_naive();

    let mut c = 0;
    for i in 0..days {
        let d = today - ChronoDuration::days(i);
        let k = d.to_string();
        if let Some(v) = history.get(code).and_then(|m| m.get(&k)) {
            if *v >= threshold_percent {
                c += 1;
            } else {
                break;
            }
        } else if d.weekday().number_from_monday() <= 5 {
            break;
        }
    }
    c
}

fn format_limit(limit: Option<f64>, limit_text: &str) -> String {
    let raw = limit_text.trim();
    if raw.contains("暂停") {
        return "暂停申购".to_string();
    }
    if !raw.is_empty() && raw != "-" {
        return raw.to_string();
    }
    match limit {
        None => "-".to_string(),
        Some(v) if v >= 100_000_000.0 => format!("{:.0}亿", v / 100_000_000.0),
        Some(v) if v >= 10_000.0 => format!("{:.0}万", v / 10_000.0),
        Some(v) => format!("{:.0}元", v),
    }
}

fn generate_report(tag: &str, funds: &[Fund], history: &HistoryMap) -> String {
    let sh_tz = FixedOffset::east_opt(8 * 3600).expect("tz");
    let now = Utc::now().with_timezone(&sh_tz);

    let mut with_premium: Vec<&Fund> = funds.iter().filter(|f| f.premium.is_some()).collect();
    with_premium.sort_by(|a, b| {
        b.premium
            .partial_cmp(&a.premium)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    let premium_count = funds
        .iter()
        .filter(|f| f.premium.unwrap_or(0.0) > 0.0)
        .count();
    let suspended_count = funds.iter().filter(|f| f.suspended).count();

    let mut opportunities: Vec<(&Fund, f64, i64)> = Vec::new();
    for f in funds {
        if let Some(p) = f.premium {
            let amount_ok = f.amount.unwrap_or(0.0) >= AMOUNT_THRESHOLD;
            let limit_ok = f.limit.map(|v| v >= LIMIT_THRESHOLD).unwrap_or(true);
            let days = consecutive_days(history, &f.code, 5.0, CONSECUTIVE_DAYS);
            if p >= PREMIUM_THRESHOLD
                && amount_ok
                && !f.suspended
                && limit_ok
                && days >= CONSECUTIVE_DAYS
            {
                opportunities.push((f, p - DEFAULT_COST, days));
            }
        }
    }
    opportunities.sort_by(|a, b| {
        b.0.premium
            .partial_cmp(&a.0.premium)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    let mut lines = Vec::new();
    lines.push(format!(
        "📊 QDII-LOF套利监控 {} {}",
        now.format("%Y-%m-%d %H:%M"),
        tag
    ));
    lines.push("════════════════════════════════════════".to_string());
    lines.push(format!(
        "📦 共 {} 只QDII | 📈 {} 只有溢价 | 📉 {} 只暂停申购",
        funds.len(),
        premium_count,
        suspended_count
    ));
    lines.push(format!("💸 默认成本: {:.2}%", DEFAULT_COST * 100.0));
    lines.push("".to_string());

    lines.push("🔥 套利机会（溢价≥5% + 成交额≥50万 + 限额≥100元）".to_string());
    if opportunities.is_empty() {
        lines.push("   暂无符合条件的套利机会 ⏳".to_string());
    } else {
        for (f, profit, days) in opportunities.iter().take(10) {
            lines.push(format!(
                "🔥 [{}]{} 溢价{:.1}% 利润{:.1}% 限额:{} 连续{}天",
                f.code,
                f.name,
                f.premium.unwrap_or(0.0) * 100.0,
                profit * 100.0,
                format_limit(f.limit, &f.limit_text),
                days
            ));
        }
    }

    lines.push("".to_string());
    lines.push("📊 溢价率TOP10".to_string());
    for (idx, f) in with_premium.iter().take(10).enumerate() {
        let p = f.premium.unwrap_or(0.0) * 100.0;
        let level = if p >= 10.0 {
            "🔴"
        } else if p >= 5.0 {
            "🟠"
        } else {
            "🟡"
        };
        let pause = if f.suspended { "🚫暂停" } else { "" };
        let days = consecutive_days(history, &f.code, 5.0, CONSECUTIVE_DAYS);
        let badge = if days >= CONSECUTIVE_DAYS {
            "✅3天"
        } else if days > 0 {
            "📅2天"
        } else {
            ""
        };
        lines.push(format!(
            "   {}. [{}]{} {}{:.1}% {} {}",
            idx + 1,
            f.code,
            f.name,
            level,
            p,
            pause,
            badge
        ));
    }

    lines.push("".to_string());
    lines.push("⚠️ 高溢价但暂不符合".to_string());
    let mut shown = 0;
    for f in with_premium.iter() {
        let p = f.premium.unwrap_or(0.0);
        if p < PREMIUM_THRESHOLD {
            continue;
        }
        let amount_ok = f.amount.unwrap_or(0.0) >= AMOUNT_THRESHOLD;
        let limit_ok = f.limit.map(|v| v >= LIMIT_THRESHOLD).unwrap_or(true);
        let days = consecutive_days(history, &f.code, 5.0, CONSECUTIVE_DAYS);
        let eligible = amount_ok && !f.suspended && limit_ok && days >= CONSECUTIVE_DAYS;
        if eligible {
            continue;
        }
        let mut reasons = Vec::new();
        if f.suspended {
            reasons.push("🚫暂停申购".to_string());
        }
        if !amount_ok {
            reasons.push(format!("💧成交额{}", f.amount.unwrap_or(0.0)));
        }
        if !limit_ok {
            reasons.push(format!("🔒限额{}", format_limit(f.limit, &f.limit_text)));
        }
        if days < CONSECUTIVE_DAYS {
            reasons.push(format!("📅连续仅{}天(需3天)", days));
        }
        lines.push(format!(
            "  [{}]{} {:>5.2}% {}",
            f.code,
            f.name,
            p * 100.0,
            reasons.join(" | ")
        ));
        shown += 1;
        if shown >= 8 {
            break;
        }
    }
    if shown == 0 {
        lines.push("  暂无".to_string());
    }

    lines.join("\n")
}

fn build_board(funds: &[Fund], history: &HistoryMap) -> BoardData {
    let mut rows: Vec<BoardRow> = funds
        .iter()
        .map(|f| BoardRow {
            code: f.code.clone(),
            name: f.name.clone(),
            rt_nav: f.rt_nav,
            rt_premium_pct: f.rt_premium_pct,
            latest_nav: f.latest_nav,
            latest_premium_pct: f.latest_premium_pct,
            price: f.price,
            change_pct: f.change_pct,
            amount_wan: f.amount.map(|a| a / 10_000.0),
            limit_text: format_limit(f.limit, &f.limit_text),
            suspended: f.suspended,
            consecutive_days: consecutive_days(history, &f.code, 5.0, CONSECUTIVE_DAYS),
            history: history_points(history, &f.code, 30),
        })
        .collect();

    rows.sort_by(|a, b| {
        b.rt_premium_pct
            .unwrap_or(-9999.0)
            .partial_cmp(&a.rt_premium_pct.unwrap_or(-9999.0))
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    BoardData {
        updated_at: Utc::now(),
        rows,
    }
}

fn history_points(history: &HistoryMap, code: &str, days: i64) -> Vec<BoardPoint> {
    let sh_tz = FixedOffset::east_opt(8 * 3600).expect("tz");
    let today = Utc::now().with_timezone(&sh_tz).date_naive();

    let mut points = Vec::new();
    for i in (0..days).rev() {
        let d = today - ChronoDuration::days(i);
        let k = d.to_string();
        if let Some(v) = history.get(code).and_then(|m| m.get(&k)) {
            points.push(BoardPoint {
                date: k,
                premium_pct: *v,
            });
        }
    }
    points
}

async fn load_state(path: &Path) -> SidecarState {
    match tokio::fs::read_to_string(path).await {
        Ok(content) => serde_json::from_str::<SidecarState>(&content).unwrap_or_default(),
        Err(_) => SidecarState::default(),
    }
}

async fn save_state(path: &Path, s: &SidecarState) {
    let tmp = path.with_extension("json.tmp");
    if let Ok(content) = serde_json::to_string_pretty(s) {
        let _ = tokio::fs::write(&tmp, content).await;
        let _ = tokio::fs::rename(tmp, path).await;
    }
}

async fn persist_run(state: &AppState, run: LastRun, board: Option<BoardData>) -> LastRun {
    let mut current = load_state(&state.state_file).await;
    current.stats.total_runs += 1;
    match run.status.as_str() {
        "ok" => current.stats.success_runs += 1,
        "timeout" => current.stats.timeout_runs += 1,
        _ => current.stats.error_runs += 1,
    }
    current.last_run = Some(run.clone());
    if let Some(b) = board {
        current.last_board = Some(b);
    }
    save_state(&state.state_file, &current).await;
    run
}

async fn index() -> Html<String> {
    Html(
        r#"<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>LOF Sidecar · Rust</title>
  <style>
    :root{--bg:#f4f7fb;--panel:#ffffff;--fg:#0d1b2a;--muted:#4f5d75;--accent:#0ea5e9;--ok:#16a34a;--err:#dc2626;--warn:#d97706;}
    .dark{--bg:#0b1220;--panel:#111b2e;--fg:#e5eefc;--muted:#a2b1cc;--accent:#38bdf8;--ok:#22c55e;--err:#f87171;--warn:#f59e0b;}
    body{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,PingFang SC,Microsoft YaHei,sans-serif;background:linear-gradient(135deg,var(--bg),#d9e4f5 140%);color:var(--fg)}
    .dark body{background:linear-gradient(135deg,var(--bg),#1a2945 140%)}
    .wrap{max-width:980px;margin:28px auto;padding:0 16px}
    .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
    .card{background:var(--panel);border-radius:16px;padding:18px;box-shadow:0 10px 28px rgba(2,8,23,.12);margin-bottom:14px}
    button,.btnlink{border:none;border-radius:10px;padding:10px 14px;cursor:pointer;color:#fff;background:var(--accent);font-weight:700;text-decoration:none;display:inline-block}
    .btn2{background:#334155}
    .grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
    .k{font-size:12px;color:var(--muted)} .v{font-size:18px;font-weight:700}
    .ok{color:var(--ok)} .err{color:var(--err)} .warn{color:var(--warn)}
    pre{white-space:pre-wrap;word-break:break-word;background:#0f172a;color:#e2e8f0;padding:14px;border-radius:12px;max-height:420px;overflow:auto}
    .dark pre{background:#020617}
    .toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    .ctrl{border:1px solid #cbd5e1;border-radius:10px;padding:9px 12px;min-width:220px;background:#fff;color:#0d1b2a}
    .dark .ctrl{background:#0f172a;color:#e2e8f0;border-color:#334155}
    table{width:100%;border-collapse:collapse;font-size:12px}
    th,td{padding:8px 6px;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:middle}
    .dark th,.dark td{border-bottom-color:#1e293b}
    tbody tr{transition:background-color .12s ease}
    tbody tr:hover{background:rgba(14,165,233,.08)}
    .dark tbody tr:hover{background:rgba(56,189,248,.14)}
    th{font-size:12px;color:var(--muted)}
    .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
    th.sortable{cursor:pointer;user-select:none}
    th.sortable:hover{color:var(--fg)}
    .histv{display:inline-block;min-width:44px;text-align:right;padding:1px 4px;border-radius:6px;margin-right:2px}
    a.flink{color:var(--accent);text-decoration:none;font-weight:600}
    a.flink:hover{text-decoration:underline}
    .tinybtn{margin-left:6px;border:none;background:#334155;color:#fff;border-radius:8px;padding:2px 7px;font-size:11px;cursor:pointer}
    .tinybtn:hover{opacity:.9}
    .modal{position:fixed;inset:0;background:rgba(2,8,23,.55);display:none;align-items:center;justify-content:center;z-index:30}
    .modal-card{width:min(860px,94vw);max-height:85vh;overflow:auto;background:var(--panel);color:var(--fg);border-radius:14px;padding:14px;box-shadow:0 16px 40px rgba(2,8,23,.35)}
    .modal-top{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}
    .histgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:10px 0}
    .chip{border-radius:8px;padding:7px 9px;background:rgba(148,163,184,.12)}
    .hist-list{display:flex;flex-wrap:wrap;gap:6px}
    @media (max-width:760px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.hide-m{display:none}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h2>LOF Sidecar · Rust</h2>
    <div>
      <a class="btnlink btn2" href="/sidecars">Sidecar &#x603b;&#x63a7;&#x53f0;</a>
      <button class="btn2" onclick="toggleTheme()">切换明暗</button>
      <button onclick="runNow()">立即运行(收盘)</button>
    </div>
  </div>

  <div class="card">
    <div class="grid">
      <div><div class="k">总运行</div><div id="total" class="v">-</div></div>
      <div><div class="k">成功</div><div id="succ" class="v ok">-</div></div>
      <div><div class="k">超时</div><div id="tout" class="v warn">-</div></div>
      <div><div class="k">失败</div><div id="err" class="v err">-</div></div>
    </div>
  </div>

  <div class="card">
    <div class="k">最后一次运行</div>
    <div id="meta" class="v">加载中...</div>
  </div>

  <div class="card">
    <div class="k">最新报告</div>
    <pre id="report">加载中...</pre>
  </div>

  <div class="card">
    <div class="toolbar">
      <div class="k">精简看板（关键字段）</div>
      <input id="kw" class="ctrl" placeholder="筛选代码/名称，如 513100 或 纳指" oninput="renderBoard()"/>
    </div>
    <div style="overflow:auto;margin-top:10px;">
      <table>
        <thead>
          <tr>
            <th class="sortable" data-key="code" data-type="str" onclick="onSort(this)">代码</th>
            <th class="sortable" data-key="name" data-type="str" onclick="onSort(this)">名称</th>
            <th class="sortable" data-key="rt_nav" data-type="num" onclick="onSort(this)">实时估值</th>
            <th class="sortable" data-key="rt_premium_pct" data-type="num" onclick="onSort(this)">实时溢价%</th>
            <th class="sortable" data-key="latest_nav" data-type="num" onclick="onSort(this)">最新估值</th>
            <th class="sortable" data-key="latest_premium_pct" data-type="num" onclick="onSort(this)">最新溢价%</th>
            <th class="sortable" data-key="price" data-type="num" onclick="onSort(this)">现价</th>
            <th class="sortable" data-key="change_pct" data-type="num" onclick="onSort(this)">涨跌%</th>
            <th class="sortable" data-key="amount_wan" data-type="num" onclick="onSort(this)">成交额(万元)</th>
            <th class="sortable" data-key="limit_text" data-type="str" onclick="onSort(this)">限额</th>
            <th class="sortable" data-key="hist_recent" data-type="num" onclick="onSort(this)">历史溢价</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
</div>

<div id="histModal" class="modal" onclick="if(event.target===this)closeHist()">
  <div class="modal-card">
    <div class="modal-top">
      <div id="histTitle" class="v" style="font-size:20px;">历史溢价详情</div>
      <button class="btn2" onclick="closeHist()">关闭</button>
    </div>
    <div class="toolbar">
      <div class="k">统计窗口</div>
      <select id="histWin" class="ctrl" onchange="renderHistModal()">
        <option value="7">近7天</option>
        <option value="14">近14天</option>
        <option value="30">近30天</option>
      </select>
    </div>
    <div id="histStats" class="histgrid"></div>
    <div class="k" style="margin:8px 0 4px;">明细（从近到远）</div>
    <div id="histSeries" class="hist-list"></div>
  </div>
</div>
<script>
const root=document.documentElement;
if(localStorage.theme==='dark'){root.classList.add('dark')}
let latestBoard=null;
let sortState={key:'rt_premium_pct',dir:'desc',type:'num'};
let histRow=null;
function toggleTheme(){root.classList.toggle('dark');localStorage.theme=root.classList.contains('dark')?'dark':'light'}
function fmt(s){try{return new Date(s).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})}catch{return s||'-'}}
function esc(s){return String(s??'').replace(/[&<>"']/g, m=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m]))}
function histClass(v){
  if(v>=5) return 'warn';
  if(v>=0) return 'ok';
  return 'err';
}
function historyVals(points, days){
  const arr=(points||[]).slice(-days).reverse();
  return arr.map(p=>Number(p.premium_pct||0));
}
function historyHtml(points, days){
  const vals=historyVals(points,days);
  if(vals.length===0) return '-';
  return vals.map(v=>`<span class="histv ${histClass(v)}">${v.toFixed(2)}%</span>`).join('');
}
function valueForSort(r, key, days){
  if(key==='hist_recent'){
    const vals=historyVals(r.history,days);
    return vals.length?vals[0]:null;
  }
  return r?.[key];
}
function cmp(a,b,type){
  if(type==='num'){
    const av=(a==null||a==='')?-Infinity:Number(a);
    const bv=(b==null||b==='')?-Infinity:Number(b);
    return av===bv?0:(av>bv?1:-1);
  }
  const as=String(a??'');
  const bs=String(b??'');
  return as.localeCompare(bs,'zh-CN');
}
function onSort(th){
  const key=th.dataset.key, type=th.dataset.type||'str';
  if(sortState.key===key){ sortState.dir = (sortState.dir==='asc'?'desc':'asc'); }
  else{ sortState={key,dir:(type==='num'?'desc':'asc'),type}; }
  renderBoard();
}
function refreshSortHeader(){
  document.querySelectorAll('th.sortable').forEach(th=>{
    const key=th.dataset.key;
    const label=th.textContent.replace(/[↑↓]$/,'');
    th.textContent = key===sortState.key ? `${label}${sortState.dir==='asc'?'↑':'↓'}` : label;
  });
}
function openHist(code){
  if(!latestBoard||!latestBoard.rows) return;
  histRow=(latestBoard.rows||[]).find(x=>String(x.code||'')===String(code||'')) || null;
  if(!histRow) return;
  document.getElementById('histTitle').textContent=`${histRow.code} ${histRow.name} 历史溢价详情`;
  document.getElementById('histModal').style.display='flex';
  renderHistModal();
}
function closeHist(){
  document.getElementById('histModal').style.display='none';
}
function renderHistModal(){
  const stat=document.getElementById('histStats');
  const series=document.getElementById('histSeries');
  if(!histRow){ stat.innerHTML=''; series.innerHTML=''; return; }
  const win=Number(document.getElementById('histWin')?.value||7);
  const pts=(histRow.history||[]).slice(-win);
  if(!pts.length){
    stat.innerHTML='<div class="k">暂无历史数据</div>';
    series.innerHTML='-';
    return;
  }
  const vals=pts.map(p=>Number(p.premium_pct||0));
  const latest=vals[vals.length-1];
  const avg=vals.reduce((a,b)=>a+b,0)/vals.length;
  const min=Math.min(...vals), max=Math.max(...vals);
  const highDays=vals.filter(v=>v>=5).length;
  stat.innerHTML=`
    <div class="chip"><div class="k">最新</div><div class="${histClass(latest)}"><b>${latest.toFixed(2)}%</b></div></div>
    <div class="chip"><div class="k">均值</div><div><b>${avg.toFixed(2)}%</b></div></div>
    <div class="chip"><div class="k">区间</div><div><b>${min.toFixed(2)}% ~ ${max.toFixed(2)}%</b></div></div>
    <div class="chip"><div class="k">>=5% 天数</div><div><b>${highDays}/${vals.length}</b></div></div>
  `;
  series.innerHTML=pts.slice().reverse().map(p=>{
    const v=Number(p.premium_pct||0);
    return `<span class="histv ${histClass(v)}">${esc(p.date)} ${v.toFixed(2)}%</span>`;
  }).join('');
}
function renderBoard(){
  const tbody=document.getElementById('rows');
  if(!latestBoard||!latestBoard.rows){tbody.innerHTML='<tr><td colspan="11">暂无看板数据，请先点一次“立即运行”。</td></tr>';return}
  const kw=(document.getElementById('kw').value||'').trim().toLowerCase();
  const days=3;
  let rows=(latestBoard.rows||[]).filter(r=>!kw || (r.code||'').toLowerCase().includes(kw) || (r.name||'').toLowerCase().includes(kw));
  rows=[...rows].sort((a,b)=>{
    const av=valueForSort(a,sortState.key,days), bv=valueForSort(b,sortState.key,days);
    const base=cmp(av,bv,sortState.type);
    return sortState.dir==='asc'?base:-base;
  });
  refreshSortHeader();
  tbody.innerHTML=rows.slice(0,80).map(r=>{
    const rp=(r.rt_premium_pct==null)?'-':Number(r.rt_premium_pct).toFixed(2);
    const lp=(r.latest_premium_pct==null)?'-':Number(r.latest_premium_pct).toFixed(2);
    const pCls=(r.rt_premium_pct??-999)>=5?'warn':((r.rt_premium_pct??-999)>=0?'ok':'');
    const ch=(r.change_pct==null)?'-':Number(r.change_pct).toFixed(2);
    const chCls=(r.change_pct??0)>0?'ok':((r.change_pct??0)<0?'err':'');
    const hist=historyHtml(r.history, days);
    const tip=(r.history||[]).slice(-days).reverse().map(x=>`${x.date}:${Number(x.premium_pct).toFixed(2)}%`).join('\\n');
    const fundUrl=`https://fund.eastmoney.com/${encodeURIComponent(String(r.code||''))}.html`;
    return `<tr>
      <td class="mono"><a class="flink" href="${fundUrl}" target="_blank" rel="noopener noreferrer">${esc(r.code)}</a></td>
      <td><a class="flink" href="${fundUrl}" target="_blank" rel="noopener noreferrer">${esc(r.name)}</a></td>
      <td>${r.rt_nav==null?'-':Number(r.rt_nav).toFixed(4)}</td>
      <td class="${pCls}">${rp}</td>
      <td>${r.latest_nav==null?'-':Number(r.latest_nav).toFixed(4)}</td>
      <td>${lp}</td>
      <td>${r.price==null?'-':Number(r.price).toFixed(3)}</td>
      <td class="${chCls}">${ch}</td>
      <td>${r.amount_wan==null?'-':Number(r.amount_wan).toFixed(0)}</td>
      <td>${esc(r.limit_text||'-')}</td>
      <td title="${esc(tip)}">${hist}<button class="tinybtn" onclick="openHist('${String(r.code||'')}')">详情</button></td>
    </tr>`;
  }).join('');
}
async function refresh(){
  const r=await fetch('/api/status'); const d=await r.json();
  document.getElementById('total').textContent=d.stats?.total_runs ?? 0;
  document.getElementById('succ').textContent=d.stats?.success_runs ?? 0;
  document.getElementById('tout').textContent=d.stats?.timeout_runs ?? 0;
  document.getElementById('err').textContent=d.stats?.error_runs ?? 0;
  const lr=d.last_run;
  if(!lr){document.getElementById('meta').textContent='暂无';document.getElementById('report').textContent='暂无';return}
  document.getElementById('meta').innerHTML=`状态: <b>${lr.status}</b> ｜ 标签: ${lr.tag} ｜ 时长: ${lr.duration_ms}ms ｜ 完成: ${fmt(lr.finished_at)} ｜ 看板更新: ${fmt(d.last_board?.updated_at)}`;
  document.getElementById('report').textContent = lr.report || (lr.error || '空输出');
  latestBoard=d.last_board;
  renderBoard();
}
async function runNow(){
  const btn=event.target; btn.disabled=true; const bak=btn.textContent; btn.textContent='运行中...';
  try{
    await fetch('/api/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({tag:'收盘'})});
    await refresh();
  }finally{btn.disabled=false; btn.textContent=bak}
}
refresh(); setInterval(refresh,10000);
</script>
</body></html>"#
            .to_string(),
    )
}
