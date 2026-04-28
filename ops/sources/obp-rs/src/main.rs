mod config;
mod proxy;

use crate::config::{load_config, save_config, Channel};
use crate::proxy::{handle_proxy, ProxyState};
use axum::{
    extract::{Path, State},
    response::Html,
    routing::{delete, get, post, put},
    Json, Router,
};
use reqwest::Client;
use std::sync::Arc;
use std::{env, net::SocketAddr};
use tokio::sync::Mutex;

const CONFIG_PATH: &str = "data/config.json";

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    std::fs::create_dir_all("data").ok();

    let channels = load_config(CONFIG_PATH);
    let state = Arc::new(ProxyState {
        client: Client::builder()
            .timeout(std::time::Duration::from_secs(60))
            .build()
            .unwrap(),
        channels: Mutex::new(channels),
        index: Mutex::new(0),
    });

    let app = Router::new()
        .route("/", get(dashboard))
        .route("/v1/chat/completions", post(handle_proxy))
        .route("/admin/channels", get(get_channels).post(add_channel))
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
    Json(serde_json::json!({ "channels": *channels, "logs": [] }))
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
    save_config(CONFIG_PATH, &channels);
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
        *ch = updated_ch;
        save_config(CONFIG_PATH, &channels);
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
    save_config(CONFIG_PATH, &channels);
    Json(serde_json::json!({ "status": "ok" }))
}
