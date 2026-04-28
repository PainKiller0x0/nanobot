use axum::{extract::State, routing::{get, post}, Json, Router};
use hex;
use reqwest::Client;
use ring::digest::{Context, SHA256};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::{env, net::SocketAddr};
use tokio::fs::{create_dir_all, remove_file, rename, File};
use tokio::io::AsyncWriteExt;

#[derive(Clone)]
struct AppState {
    client: Client,
}

#[derive(Deserialize)]
struct DownloadReq {
    url: String,
    target_path: String,
    max_bytes: usize,
}

#[derive(Serialize)]
struct DownloadRes {
    success: bool,
    path: Option<String>,
    error: Option<String>,
}

#[derive(Deserialize)]
struct VerifyReq {
    content: String,
}

#[derive(Serialize)]
struct VerifyRes {
    success: bool,
    body: Option<String>,
    error: Option<String>,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    let state = AppState {
        client: Client::builder()
            .timeout(std::time::Duration::from_secs(120))
            .build()
            .unwrap(),
    };

    let app = Router::new()
        .route("/health", get(health_handler))
        .route("/download", post(download_handler))
        .route("/verify", post(verify_handler))
        .with_state(state);

    let host = env::var("QQ_SIDECAR_HOST").unwrap_or_else(|_| "0.0.0.0".to_string());
    let port: u16 = env::var("QQ_SIDECAR_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8092);
    let addr: SocketAddr = format!("{}:{}", host, port).parse().unwrap();
    println!("QQ-Sidecar-RS listening on {}", addr);
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn health_handler() -> &'static str {
    "ok"
}

async fn download_handler(
    State(state): State<AppState>,
    Json(payload): Json<DownloadReq>,
) -> Json<DownloadRes> {
    let target = Path::new(&payload.target_path);
    if let Some(p) = target.parent() {
        let _ = create_dir_all(p).await;
    }

    let tmp_path = target.with_extension("part");

    let mut resp = match state.client.get(&payload.url).send().await {
        Ok(r) => {
            if !r.status().is_success() {
                return Json(DownloadRes {
                    success: false,
                    path: None,
                    error: Some(format!("HTTP {}", r.status())),
                });
            }
            r
        }
        Err(e) => {
            return Json(DownloadRes {
                success: false,
                path: None,
                error: Some(e.to_string()),
            })
        }
    };

    let mut file = match File::create(&tmp_path).await {
        Ok(f) => f,
        Err(e) => {
            return Json(DownloadRes {
                success: false,
                path: None,
                error: Some(e.to_string()),
            })
        }
    };

    let mut downloaded = 0;
    while let Ok(Some(chunk)) = resp.chunk().await {
        downloaded += chunk.len();
        if downloaded > payload.max_bytes {
            let _ = file.flush().await;
            let _ = remove_file(&tmp_path).await;
            return Json(DownloadRes {
                success: false,
                path: None,
                error: Some("Max bytes exceeded".to_string()),
            });
        }
        if let Err(e) = file.write_all(&chunk).await {
            let _ = remove_file(&tmp_path).await;
            return Json(DownloadRes {
                success: false,
                path: None,
                error: Some(e.to_string()),
            });
        }
    }
    let _ = file.flush().await;

    match rename(&tmp_path, target).await {
        Ok(_) => Json(DownloadRes {
            success: true,
            path: Some(payload.target_path),
            error: None,
        }),
        Err(e) => {
            let _ = remove_file(&tmp_path).await;
            Json(DownloadRes {
                success: false,
                path: None,
                error: Some(e.to_string()),
            })
        }
    }
}

async fn verify_handler(Json(payload): Json<VerifyReq>) -> Json<VerifyRes> {
    let raw = payload.content.replace("\r\n", "\n").replace("\r", "\n");

    if !raw.starts_with("NBRAW1-SHA256:") {
        if raw.contains("NBRAW1-SHA256:") {
            println!("[Verify] Failed: Prefix found but not at start");
            return Json(VerifyRes {
                success: false,
                body: None,
                error: Some("Prefix not at start".to_string()),
            });
        }
        return Json(VerifyRes {
            success: true,
            body: Some(raw.trim().to_string()),
            error: None,
        });
    }

    let parts: Vec<&str> = raw.splitn(2, '\n').collect();
    if parts.len() != 2 {
        return Json(VerifyRes {
            success: false,
            body: None,
            error: Some("Invalid format".to_string()),
        });
    }

    let declared = parts[0]["NBRAW1-SHA256:".len()..].trim().to_lowercase();
    let body = parts[1];

    let mut ctx = Context::new(&SHA256);
    ctx.update(body.as_bytes());
    let computed = hex::encode(ctx.finish().as_ref());

    if computed != declared {
        // 尝试另一种兼容模式：去掉 body 首部的第一个换行符（如果存在）
        let alt_body = if body.starts_with('\n') {
            &body[1..]
        } else {
            body
        };
        let mut ctx_alt = Context::new(&SHA256);
        ctx_alt.update(alt_body.as_bytes());
        let computed_alt = hex::encode(ctx_alt.finish().as_ref());

        if computed_alt == declared {
            return Json(VerifyRes {
                success: true,
                body: Some(alt_body.to_string()),
                error: None,
            });
        }

        println!(
            "[Verify] Hash mismatch! Declared: {}, Computed: {}, ComputedAlt: {}",
            declared, computed, computed_alt
        );
        return Json(VerifyRes {
            success: false,
            body: None,
            error: Some(format!("Hash mismatch {} != {}", declared, computed)),
        });
    }

    Json(VerifyRes {
        success: true,
        body: Some(body.to_string()),
        error: None,
    })
}
