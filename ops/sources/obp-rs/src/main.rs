mod config;
mod proxy;
mod stats;

use crate::config::{
    load_config, load_router_config, save_config, save_router_config, Channel, RouterConfig,
};
use crate::proxy::{handle_proxy, ProxyState};
use crate::stats::{load_stats, pricing_snapshot, save_stats, UsageStats};
use axum::{
    extract::{Path, State},
    response::Html,
    routing::{get, post, put},
    Json, Router,
};
use reqwest::Client;
use std::sync::Arc;
use std::{env, net::SocketAddr};
use tokio::sync::Mutex;

const CONFIG_PATH: &str = "data/config.json";
const ROUTER_PATH: &str = "data/router.json";
const STATS_PATH: &str = "data/stats.json";

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    std::fs::create_dir_all("data").ok();

    let config_path = env::var("OBP_CONFIG_PATH").unwrap_or_else(|_| CONFIG_PATH.to_string());
    let router_path = env::var("OBP_ROUTER_PATH").unwrap_or_else(|_| ROUTER_PATH.to_string());
    let stats_path = env::var("OBP_STATS_PATH").unwrap_or_else(|_| STATS_PATH.to_string());
    let channels = load_config(&config_path);
    let router = load_router_config(&router_path);
    let stats = load_stats(&stats_path);
    let state = Arc::new(ProxyState {
        client: Client::builder()
            .timeout(std::time::Duration::from_secs(60))
            .build()
            .unwrap(),
        channels: Mutex::new(channels),
        router: Mutex::new(router),
        stats: Mutex::new(stats),
        index: Mutex::new(0),
        config_path,
        router_path,
        stats_path,
    });

    let app = Router::new()
        .route("/", get(dashboard))
        .route("/v1/chat/completions", post(handle_proxy))
        .route("/admin/channels", get(get_channels).post(add_channel))
        .route("/admin/stats", get(get_stats).delete(clear_stats))
        .route("/admin/router", get(get_router).put(update_router))
        .route(
            "/admin/channels/{id}",
            put(update_channel).delete(delete_channel),
        )
        .with_state(state);

    let host = env::var("OBP_HOST").unwrap_or_else(|_| "0.0.0.0".to_string());
    let port: u16 = env::var("OBP_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8000);
    let addr: SocketAddr = format!("{}:{}", host, port).parse().unwrap();
    println!("OBP-RS listening on {}", addr);
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn dashboard() -> Html<&'static str> {
    Html(include_str!("index.html"))
}

async fn get_channels(State(state): State<Arc<ProxyState>>) -> Json<serde_json::Value> {
    let channels = state.channels.lock().await;
    let router = state.router.lock().await;
    let stats = state.stats.lock().await;
    Json(serde_json::json!({
        "channels": redacted_channels(&channels),
        "router": &*router,
        "stats": &*stats,
        "pricing": pricing_snapshot(),
        "logs": &stats.recent,
    }))
}

fn redacted_channels(channels: &[Channel]) -> Vec<Channel> {
    channels
        .iter()
        .cloned()
        .map(|mut ch| {
            if !ch.key.trim().is_empty() {
                ch.key = "***".to_string();
            }
            ch
        })
        .collect()
}

async fn get_stats(State(state): State<Arc<ProxyState>>) -> Json<UsageStats> {
    let stats = state.stats.lock().await;
    Json(stats.clone())
}

async fn clear_stats(State(state): State<Arc<ProxyState>>) -> Json<serde_json::Value> {
    let mut stats = state.stats.lock().await;
    *stats = UsageStats::default();
    save_stats(&state.stats_path, &stats);
    Json(serde_json::json!({ "status": "ok" }))
}

async fn get_router(State(state): State<Arc<ProxyState>>) -> Json<RouterConfig> {
    let router = state.router.lock().await;
    Json(router.clone())
}

async fn update_router(
    State(state): State<Arc<ProxyState>>,
    Json(router): Json<RouterConfig>,
) -> Json<RouterConfig> {
    let mut current = state.router.lock().await;
    *current = router.clone();
    save_router_config(&state.router_path, &router);
    Json(router)
}

async fn add_channel(
    State(state): State<Arc<ProxyState>>,
    Json(mut ch): Json<Channel>,
) -> Json<Channel> {
    let mut channels = state.channels.lock().await;
    ch.id = Some(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs(),
    );
    channels.push(ch.clone());
    save_config(&state.config_path, &channels);
    Json(ch)
}

async fn update_channel(
    State(state): State<Arc<ProxyState>>,
    Path(id): Path<u64>,
    Json(mut updated_ch): Json<Channel>,
) -> Json<serde_json::Value> {
    let mut channels = state.channels.lock().await;
    if let Some(ch) = channels.iter_mut().find(|c| c.id == Some(id)) {
        updated_ch.id = Some(id);
        if updated_ch.key.trim().is_empty() || updated_ch.key.trim() == "***" {
            updated_ch.key = ch.key.clone();
        }
        *ch = updated_ch;
        save_config(&state.config_path, &channels);
        return Json(serde_json::json!({ "status": "ok" }));
    }
    Json(serde_json::json!({ "status": "not_found" }))
}

async fn delete_channel(
    State(state): State<Arc<ProxyState>>,
    Path(id): Path<u64>,
) -> Json<serde_json::Value> {
    let mut channels = state.channels.lock().await;
    channels.retain(|c| c.id != Some(id));
    save_config(&state.config_path, &channels);
    Json(serde_json::json!({ "status": "ok" }))
}
