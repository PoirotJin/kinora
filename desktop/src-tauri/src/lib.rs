//! MotionFarm Desktop: a small native shell around the local renderer
//! (`src/renderer`). It uses the operating system's WebView, so no browser
//! engine is bundled. All cloud traffic goes through `cloud` / `cloud_binary`
//! below, limited by `policy`; the renderer has no direct network access.
mod credentials;
mod server;
mod policy;
mod session;
mod updates;
mod releases;
mod single_instance;
#[cfg(target_os = "macos")]
mod installations;
mod account;
#[cfg(any(target_os = "linux", target_os = "macos"))]
mod download_staging;
use updates::Updates;

use policy::Kind;
use std::{path::{Path, PathBuf}, sync::{Arc, RwLock, atomic::{AtomicBool, Ordering}}};
use serde::Deserialize;
use serde_json::{json, Value};
use std::time::Duration;
use tauri::{ipc::Response, AppHandle, Manager, State, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

struct Cloud {
    client: reqwest::Client,
    connection: RwLock<Option<Arc<server::Connection>>>,
    directory: PathBuf,
    preference_dir: Option<PathBuf>,
    preference_unavailable: AtomicBool,
}

impl Cloud {
    fn current(&self) -> Result<Arc<server::Connection>, String> {
        self.connection.read().unwrap().clone().ok_or_else(|| "请先配置服务器地址".into())
    }
    fn current_for(&self, origin: &str) -> Result<Arc<server::Connection>, String> {
        let connection = self.current()?;
        if connection.origin != origin { return Err("服务器已切换，请重试".into()); }
        Ok(connection)
    }

    fn with_active_connection<T>(
        &self,
        expected: &Arc<server::Connection>,
        action: impl FnOnce() -> T,
    ) -> Option<T> {
        let guard = self.connection.read().ok()?;
        guard.as_ref().filter(|active| Arc::ptr_eq(active, expected))?;
        Some(action())
    }

    fn prepare_download(
        &self,
        connection: &Arc<server::Connection>,
        cookie: Option<&str>,
        url: &str,
    ) -> Result<reqwest::RequestBuilder, &'static str> {
        self.with_download_session(connection, cookie, || {
            let mut builder = self.client.get(url).timeout(Duration::from_secs(120));
            if let Some(cookie) = cookie {
                builder = builder.header("Cookie", cookie);
            }
            builder
        })
    }

    fn with_download_session<T>(
        &self,
        connection: &Arc<server::Connection>,
        cookie: Option<&str>,
        action: impl FnOnce() -> T,
    ) -> Result<T, &'static str> {
        const SESSION_CHANGED: &str = "下载会话已变更，请重试";
        self.with_active_connection(connection, || connection.session.with_matching_cookie(cookie, action))
            .flatten()
            .ok_or(SESSION_CHANGED)
    }
}

#[derive(Deserialize)]
struct CloudRequest {
    server: String,
    method: Option<String>,
    path: String,
    query: Option<Value>,
    body: Option<Value>,
}

#[derive(Deserialize)]
struct DownloadRequest {
    server: String,
    path: String,
    query: Option<Value>,
    name: Option<String>,
}

fn platform() -> &'static str {
    // Same names the renderer already uses.
    match std::env::consts::OS {
        "macos" => "darwin",
        "windows" => "win32",
        other => other,
    }
}

fn snapshot(app: &AppHandle) -> Value {
    let updates = app.state::<Updates>();
    let pending = updates.0.lock().unwrap();
    let cloud = app.state::<Cloud>();
    let connection = cloud.current().ok();
    json!({
        "server": connection.as_ref().map(|c| c.origin.as_str()),
        "serverConfigured": connection.is_some(),
        "serverPreferenceUnavailable": connection.is_none() && cloud.preference_unavailable.load(Ordering::Relaxed),
        "version": app.package_info().version.to_string(),
        "packaged": !cfg!(debug_assertions),
        "platform": platform(),
        "update": pending.message,
        "updatePhase": pending.phase,
        "releaseNotes": releases::notes(),
        "releaseNotesUnread": releases::unread(app),
    })
}

fn failure(status: u16, error: &str, must_change: bool) -> Value {
    json!({ "ok": false, "status": status, "error": error, "must_change": must_change })
}

#[cfg(test)]
mod logout_tests {
    use super::*;
    use std::{io::{BufRead, BufReader, Write}, net::TcpListener};

    #[test]
    fn failed_logout_clears_the_local_session() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            let headers: Vec<_> = BufReader::new(&stream).lines().map(Result::unwrap)
                .take_while(|line| !line.is_empty()).collect();
            assert!(headers.iter().any(|line| line.eq_ignore_ascii_case("cookie: mf_session=logout-test")));
            stream.write_all(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nSet-Cookie: mf_session=ignored; Max-Age=3600\r\n\r\n").unwrap();
        });
        let dir = tempfile::tempdir().unwrap();
        let connection = server::connection(dir.path(), origin.clone());
        connection.session.apply(std::iter::once("mf_session=logout-test; Max-Age=3600"));
        let cloud = Cloud {
            client: reqwest::Client::builder().no_proxy().build().unwrap(),
            connection: RwLock::new(Some(connection.clone())),
            directory: dir.path().to_owned(),
            preference_dir: None,
            preference_unavailable: AtomicBool::new(false),
        };
        let request = CloudRequest {
            server: origin.clone(), method: Some("POST".into()), path: "/api/auth/logout".into(),
            query: None, body: Some(json!({})),
        };
        let response = tauri::async_runtime::block_on(send(&cloud, &request, Kind::Json)).unwrap();
        assert_eq!(response.status(), reqwest::StatusCode::SERVICE_UNAVAILABLE);
        server.join().unwrap();
        assert_eq!(connection.session.cookie_header(), None);
        assert_eq!(server::connection(dir.path(), origin).session.cookie_header(), None);
    }

    #[test]
    fn late_password_response_cannot_replace_new_login() {
        for switch_server in [false, true] {
            use std::sync::mpsc;
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let origin = format!("http://{}", listener.local_addr().unwrap());
            let (started, password_request) = mpsc::channel();
            let (release, allow_password_response) = mpsc::channel();
            let accept = move || {
                let (stream, _) = listener.accept().unwrap();
                stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
                let headers: Vec<_> = BufReader::new(&stream).lines().map(Result::unwrap)
                    .take_while(|line| !line.is_empty()).collect();
                (stream, headers)
            };
            let peer = std::thread::spawn(move || {
                let (mut password, headers) = accept();
                assert!(headers[0].starts_with("POST /api/auth/password "));
                started.send(()).unwrap();
                let (mut login, headers) = accept();
                assert!(headers[0].starts_with("POST /api/auth/login "));
                login.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\nSet-Cookie: mf_session=new-login; Max-Age=3600\r\n\r\n").unwrap();
                allow_password_response.recv_timeout(Duration::from_secs(5)).unwrap();
                password.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\nSet-Cookie: mf_session=old-password; Max-Age=3600\r\n\r\n").unwrap();
            });
            let dir = tempfile::tempdir().unwrap();
            let connection = server::connection(dir.path(), origin.clone());
            connection.session.apply(["mf_session=old-login; Max-Age=3600"].into_iter());
            let cloud = Arc::new(Cloud {
                client: reqwest::Client::builder().no_proxy().build().unwrap(),
                connection: RwLock::new(Some(connection.clone())),
                directory: dir.path().to_owned(),
                preference_dir: None,
                preference_unavailable: AtomicBool::new(false),
            });
            let request = |path: &str| CloudRequest {
                server: origin.clone(), method: Some("POST".into()), path: path.into(),
                query: None, body: Some(json!({})),
            };
            let pending_cloud = cloud.clone();
            let password = request("/api/auth/password");
            let pending = tauri::async_runtime::spawn(async move { send(&pending_cloud, &password, Kind::Json).await });
            password_request.recv_timeout(Duration::from_secs(5)).unwrap();
            connection.session.clear();
            if switch_server {
                *cloud.connection.write().unwrap() = Some(server::connection(dir.path(), "https://other.example".into()));
                *cloud.connection.write().unwrap() = Some(server::connection(dir.path(), origin.clone()));
            }
            let response = tauri::async_runtime::block_on(send(&cloud, &request("/api/auth/login"), Kind::Json)).unwrap();
            assert!(response.status().is_success());
            release.send(()).unwrap();
            tauri::async_runtime::block_on(pending).unwrap().unwrap();
            peer.join().unwrap();
            assert_eq!(cloud.current().unwrap().session.cookie_header().as_deref(), Some("mf_session=new-login"));
            assert_eq!(server::connection(dir.path(), origin).session.cookie_header().as_deref(), Some("mf_session=new-login"));
        }
    }

    #[test]
    fn late_unauthorized_response_does_not_invalidate_rotated_password_session() {
        use std::sync::mpsc;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let (started, old_request) = mpsc::channel();
        let (release, respond) = mpsc::channel();
        let peer = std::thread::spawn(move || {
            let accept = || {
                let (stream, _) = listener.accept().unwrap();
                stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
                let headers: Vec<_> = BufReader::new(&stream).lines().map(Result::unwrap)
                    .take_while(|line| !line.is_empty()).collect();
                assert!(headers.iter().any(|line| line.eq_ignore_ascii_case("cookie: mf_session=original")));
                (stream, headers)
            };
            let (mut old, headers) = accept();
            assert!(headers[0].starts_with("GET /api/auth/me "));
            started.send(()).unwrap();
            let (mut password, headers) = accept();
            assert!(headers[0].starts_with("POST /api/auth/password "));
            password.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\nSet-Cookie: mf_session=rotated; Max-Age=3600\r\n\r\n").unwrap();
            respond.recv_timeout(Duration::from_secs(5)).unwrap();
            old.write_all(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
        });
        let dir = tempfile::tempdir().unwrap();
        let connection = server::connection(dir.path(), origin.clone());
        connection.session.apply(["mf_session=original; Max-Age=3600"].into_iter());
        let cloud = Arc::new(Cloud {
            client: reqwest::Client::builder().no_proxy().build().unwrap(),
            connection: RwLock::new(Some(connection.clone())),
            directory: dir.path().to_owned(),
            preference_dir: None,
            preference_unavailable: AtomicBool::new(false),
        });
        let request = |method: &str, path: &str| CloudRequest {
            server: origin.clone(), method: Some(method.into()), path: path.into(), query: None, body: None,
        };
        let pending_cloud = cloud.clone();
        let old = request("GET", "/api/auth/me");
        let pending = tauri::async_runtime::spawn(async move { send(&pending_cloud, &old, Kind::Json).await });
        old_request.recv_timeout(Duration::from_secs(5)).unwrap();
        let password = request("POST", "/api/auth/password");
        assert!(tauri::async_runtime::block_on(send(&cloud, &password, Kind::Json)).unwrap().status().is_success());
        release.send(()).unwrap();
        let response = tauri::async_runtime::block_on(pending).unwrap();
        peer.join().unwrap();
        assert_eq!(response.unwrap_err()["stale_auth"], true);
        assert_eq!(connection.session.cookie_header().as_deref(), Some("mf_session=rotated"));
    }
}

/// Sends an allow-listed request with the session cookie and the headers the
/// server requires for browser writes. Redirects are refused.
async fn send(cloud: &Cloud, request: &CloudRequest, want: Kind) -> Result<reqwest::Response, Value> {
    let connection = cloud.current_for(&request.server).map_err(|e| failure(0, &e, false))?;
    let resolved = policy::resolve(request.method.as_deref(), &request.path, request.query.as_ref(), &connection.origin)
        .map_err(|_| failure(0, "操作失败，请重试", false))?;
    if resolved.kind != want {
        return Err(failure(0, "操作失败，请重试", false));
    }
    let body_limit = policy::request_body_limit(&request.path);
    let timeout = Duration::from_secs(if body_limit > 64 * 1024 || want == Kind::Binary { 90 } else { 20 });
    let mut builder = if resolved.post { cloud.client.post(&resolved.url) } else { cloud.client.get(&resolved.url) }
        .timeout(timeout)
        .header("X-MotionFarm-Request", "1")
        .header("Origin", &connection.origin)
        .header("Accept", if want == Kind::Binary { "application/octet-stream" } else { "application/json" });
    if resolved.post {
        let body = serde_json::to_vec(request.body.as_ref().unwrap_or(&json!({}))).unwrap_or_default();
        if body.len() > body_limit {
            return Err(failure(413, "请求过大", false));
        }
        builder = builder.header("Content-Type", "application/json").body(body);
    }
    let logout = resolved.post && request.path == "/api/auth/logout";
    // Logout captures the outgoing cookie and clears local state together.
    let (cookie, revision) = cloud.with_active_connection(&connection, || connection.session.begin_request(logout))
        .ok_or_else(|| failure(0, "服务器已切换，请重试", false))?;
    if let Some(cookie) = cookie {
        builder = builder.header("Cookie", cookie);
    }
    let response = builder.send().await.map_err(|error| {
        let message = if error.is_timeout() { "云端响应超时，请稍后重试" } else { "无法连接 kinora 云端，请检查网络" };
        failure(0, message, false)
    })?;
    if !logout && matches!(response.status(), reqwest::StatusCode::UNAUTHORIZED | reqwest::StatusCode::FORBIDDEN) {
        let cookies: Vec<_> = response.headers().get_all("set-cookie").iter().cloned().collect();
        // A password change can rotate the cookie while the account stays signed in.
        // Check after reading the response body, which can also be delayed.
        return Err(session_error(response, None, || {
            cloud.with_active_connection(&connection, || {
                connection.session.apply_if_current(revision, cookies.iter().filter_map(|header| header.to_str().ok()))
            }) == Some(true)
        }).await);
    }
    if !logout {
        // An older reply may target this same server after logout or a reconnect.
        let _ = cloud.with_active_connection(&connection, || {
            connection.session.apply_if_current(
                revision, response.headers().get_all("set-cookie").iter().filter_map(|v| v.to_str().ok()),
            );
        });
    }
    Ok(response)
}

async fn error_body(response: reqwest::Response, batch_ids: Option<&[String]>) -> Value {
    let status = response.status().as_u16();
    let data: Value = response.json().await.unwrap_or(Value::Null);
    let message = data.get("error").and_then(Value::as_str)
        .filter(|message| !message.trim().is_empty())
        .map(str::to_string).unwrap_or_else(|| format!("请求失败（{status}）"));
    let mut result = failure(status, &message, data.get("must_change").and_then(Value::as_bool).unwrap_or(false));
    if matches!(status, 400 | 422 | 503) {
        if let Some(batch) = batch_ids.and_then(|ids| validate_batch_error(&data, ids)) {
            result["batch"] = json!(batch);
        }
    }
    result
}

async fn session_error(response: reqwest::Response, batch_ids: Option<&[String]>, current: impl FnOnce() -> bool) -> Value {
    let mut message = error_body(response, batch_ids).await;
    let current = current();
    if !current && (message["status"] == 401 || message["must_change"] == true) {
        return json!({ "ok": false, "stale_auth": true, "error": "会话已切换，请重试" });
    }
    if !current {
        if let Some(fields) = message.as_object_mut() {
            fields.remove("batch");
        }
    }
    message
}

#[tauri::command]
fn status(app: AppHandle) -> Value {
    let cloud = app.state::<Cloud>();
    {
        let mut connection = cloud.connection.write().unwrap();
        if connection.is_none() {
            let origin = server::load_origin(&cloud.directory, cloud.preference_dir.as_deref());
            cloud.preference_unavailable.store(origin.is_err(), Ordering::Relaxed);
            *connection = origin.ok().flatten().map(|origin| server::connection(&cloud.directory, origin));
        }
    }
    snapshot(&app)
}

#[tauri::command]
fn configure_server(app: AppHandle, state: State<'_, Cloud>, address: String) -> Result<Value, String> {
    let origin = server::normalize(&address)?;
    let updates = app.state::<Updates>();
    let pending = updates.0.lock().unwrap();
    if pending.phase == "installing" {
        return Err("更新正在安装，请在重启后更改服务器".into());
    }
    {
        let mut connection = state.connection.write().unwrap();
        server::save(&state.directory, state.preference_dir.as_deref(), &origin)?;
        *connection = Some(server::connection(&state.directory, origin));
    }
    drop(pending);
    for name in ["workbench", "account"] {
        if let Some(window) = app.get_webview_window(name) { let _ = window.close(); }
    }
    Ok(snapshot(&app))
}

#[tauri::command]
async fn cloud(state: State<'_, Cloud>, request: CloudRequest) -> Result<Value, ()> {
    let response = match send(&state, &request, Kind::Json).await {
        Ok(response) => response,
        Err(error) => return Ok(error),
    };
    if !response.status().is_success() {
        return Ok(error_body(response, None).await);
    }
    let status = response.status().as_u16();
    Ok(match response.json::<Value>().await {
        Ok(data) => json!({ "ok": true, "status": status, "data": data }),
        Err(_) => failure(status, "云端返回了无效数据", false),
    })
}

/// Binary payloads (motion previews, robot meshes) go back as a raw ArrayBuffer.
#[tauri::command]
async fn cloud_binary(state: State<'_, Cloud>, request: CloudRequest) -> Result<Response, Value> {
    let response = send(&state, &request, Kind::Binary).await?;
    if !response.status().is_success() {
        return Err(error_body(response, None).await);
    }
    if response.content_length().is_some_and(|n| n > 64 * 1024 * 1024) {
        return Err(failure(413, "云端数据过大", false));
    }
    let bytes = response.bytes().await.map_err(|_| failure(0, "下载数据中断，请重试", false))?;
    if bytes.len() > 64 * 1024 * 1024 {
        return Err(failure(413, "云端数据过大", false));
    }
    Ok(Response::new(bytes.to_vec()))
}

#[tauri::command]
async fn download(app: AppHandle, state: State<'_, Cloud>, request: DownloadRequest) -> Result<Value, ()> {
    let connection = match state.current_for(&request.server) { Ok(c) => c, Err(e) => return Ok(json!({ "error": e })) };
    let Ok(url) = policy::resolve_download(&request.path, request.query.as_ref(), &connection.origin) else {
        return Ok(json!({ "error": "下载不允许" }));
    };
    // The native picker may stay open while the connection or login changes.
    let cookie = connection.session.cookie_header();
    let name = policy::safe_file_name(request.name.as_deref());
    let Some(window) = app.get_webview_window("main") else {
        return Ok(json!({ "error": "找不到保存窗口，请重试" }));
    };
    let dialog = app.clone();
    let target = tauri::async_runtime::spawn_blocking(move || -> Result<_, &'static str> {
        let extension = if name.ends_with(".zip") { "zip" } else if name.ends_with(".bvh") { "bvh" } else { "npz" };
        let mut picker = dialog.dialog().file().set_parent(&window)
            .set_title("保存 kinora 文件").set_file_name(&name).add_filter("文件", &[extension]);
        if let Ok(dir) = dialog.path().download_dir() {
            picker = picker.set_directory(dir);
        }
        picker.blocking_save_file().map(|selected| {
            let path = selected.into_path().map_err(|_| DOWNLOAD_WRITE_ERROR)?;
            let destination = DownloadDestination::capture(&path)?;
            Ok((path, destination))
        }).transpose()
    }).await;
    let (path, destination) = match target {
        Ok(Ok(Some(selection))) => selection,
        Ok(Ok(None)) => return Ok(json!({ "cancelled": true })),
        Ok(Err(error)) => return Ok(json!({ "error": error })),
        Err(_) => return Ok(json!({ "error": "无法打开保存窗口，请重试" })),
    };
    let builder = match state.prepare_download(&connection, cookie.as_deref(), &url) {
        Ok(builder) => builder,
        Err(error) => return Ok(json!({ "error": error })),
    };
    let response = match builder.send().await {
        Ok(response) => response,
        Err(_) => return Ok(json!({ "error": "下载失败：无法连接云端" })),
    };
    let format = DownloadFormat::for_request(&request.path, request.query.as_ref());
    if !response.status().is_success() {
        let batch_ids = match &format {
            DownloadFormat::MotionBatch { ids, .. } => Some(ids.as_slice()),
            _ => None,
        };
        return Ok(session_error(response, batch_ids,
            || state.with_download_session(&connection, cookie.as_deref(), || ()).is_ok()).await);
    }
    let (bytes, batch) = match write_download(&path, response, format, destination, move |destination, staging, path| {
        let cloud = app.state::<Cloud>();
        cloud.with_download_session(&connection, cookie.as_deref(), || destination.persist(staging, path))?
    }).await {
        Ok(bytes) => bytes,
        Err(error) => return Ok(json!({ "error": error })),
    };
    let saved = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    Ok(json!({ "saved": saved, "bytes": bytes, "batch": batch }))
}

#[derive(Clone)]
enum DownloadFormat {
    Archive,
    Snapshot { id: String, batch: usize },
    MotionBatch { extension: &'static str, ids: Vec<String> },
    Bvh,
    Npz,
}

impl DownloadFormat {
    fn for_request(path: &str, query: Option<&Value>) -> Self {
        let bvh = query.and_then(|query| query.get("format")).and_then(Value::as_str) == Some("bvh");
        if path == "/api/harness/export" {
            let ids = query.and_then(|query| query.get("ids")).and_then(Value::as_str).unwrap_or_default();
            Self::MotionBatch {
                extension: if bvh { "bvh" } else { "npz" },
                ids: ids.split(',').map(str::to_owned).collect(),
            }
        } else if path.starts_with("/api/harness/motions/") {
            if bvh { Self::Bvh } else { Self::Npz }
        } else if let Some(id) = path.strip_prefix("/api/harness/personal-assets/snapshots/")
            .and_then(|part| part.strip_suffix("/download"))
        {
            let batch = query.and_then(|query| query.get("batch")).and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok()).unwrap_or_default();
            Self::Snapshot { id: id.to_owned(), batch }
        } else {
            Self::Archive
        }
    }

    fn matches_header(&self, header: &[u8]) -> bool {
        match self {
            Self::Archive | Self::Snapshot { .. } | Self::MotionBatch { .. } | Self::Npz =>
                header.starts_with(b"PK\x03\x04") || header.starts_with(b"PK\x05\x06"),
            Self::Bvh => header.strip_prefix(b"HIERARCHY").and_then(|tail| tail.first())
                .is_some_and(u8::is_ascii_whitespace),
        }
    }
}

fn validate_download_archive<R: std::io::Read + std::io::Seek>(file: R) -> Result<zip::ZipArchive<R>, &'static str> {
    const INVALID_ARCHIVE: &str = "云端返回的压缩文件不完整或已损坏，请重试";
    let mut archive = zip::ZipArchive::new(file).map_err(|_| INVALID_ARCHIVE)?;
    if archive.is_empty() {
        return Err(INVALID_ARCHIVE);
    }
    let directory = archive.central_directory_start();
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(|_| INVALID_ARCHIVE)?;
        let data_end = entry.data_start().and_then(|start| start.checked_add(entry.compressed_size()));
        let size = entry.size();
        // Reading through EOF verifies the entry checksum without retaining its contents.
        if !data_end.is_some_and(|end| end <= directory)
            || std::io::copy(&mut entry, &mut std::io::sink()).ok() != Some(size)
        {
            return Err(INVALID_ARCHIVE);
        }
    }
    Ok(archive)
}

#[derive(Deserialize)]
struct SnapshotManifest {
    schema: u32,
    id: String,
    items: Vec<SnapshotMember>,
    batches: Vec<[usize; 2]>,
}

#[derive(Deserialize)]
struct SnapshotMember {
    motion_id: String,
    bytes: u64,
    sha256: String,
}

#[derive(Deserialize)]
struct SnapshotBatchHeader {
    batch: usize,
    batches: usize,
    start: usize,
    end: usize,
}

fn validate_snapshot_batch(
    mut archive: zip::ZipArchive<std::fs::File>,
    expected_id: &str,
    expected_batch: usize,
) -> Result<(), &'static str> {
    use sha2::{Digest, Sha256};
    use std::collections::HashSet;

    const INVALID: &str = "云端返回的快照批次与所选快照或文件清单不一致，请重试";
    fn read_json<T: serde::de::DeserializeOwned>(
        archive: &mut zip::ZipArchive<std::fs::File>,
        name: &str,
        max_bytes: u64,
    ) -> Result<T, &'static str> {
        let entry = archive.by_name(name).map_err(|_| INVALID)?;
        if !entry.is_file() || entry.size() > max_bytes {
            return Err(INVALID);
        }
        serde_json::from_reader(entry).map_err(|_| INVALID)
    }

    let manifest: SnapshotManifest = read_json(&mut archive, "manifest.json", 2 * 1024 * 1024)?;
    let details: SnapshotBatchHeader = read_json(&mut archive, "batch.json", 4096)?;
    if manifest.schema != 1 || manifest.id != expected_id || manifest.items.len() > 1000
        || details.batch != expected_batch || details.batches != manifest.batches.len()
    {
        return Err(INVALID);
    }
    let mut next_start = 0;
    for [start, end] in &manifest.batches {
        if *start != next_start || *end <= *start || *end > manifest.items.len() {
            return Err(INVALID);
        }
        next_start = *end;
    }
    let &[start, end] = manifest.batches.get(expected_batch.checked_sub(1).ok_or(INVALID)?).ok_or(INVALID)?;
    if next_start != manifest.items.len() || (details.start, details.end) != (start, end)
        || archive.len() != end - start + 2
    {
        return Err(INVALID);
    }
    let mut seen = HashSet::new();
    for member in &manifest.items[start..end] {
        let id = &member.motion_id;
        if id.is_empty() || id.len() > 96 || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
            || !seen.insert(id)
        {
            return Err(INVALID);
        }
        let mut entry = archive.by_name(&format!("motions/{id}.npz")).map_err(|_| INVALID)?;
        if !entry.is_file() || entry.size() == 0 || entry.size() != member.bytes {
            return Err(INVALID);
        }
        let mut digest = Sha256::new();
        let bytes = std::io::copy(&mut entry, &mut digest).map_err(|_| INVALID)?;
        if bytes != member.bytes || format!("{:x}", digest.finalize()) != member.sha256 {
            return Err(INVALID);
        }
    }
    Ok(())
}

fn validate_batch_member(entry: zip::read::ZipFile<'_, std::fs::File>, extension: &str) -> Option<()> {
    use std::io::{Cursor, Read};

    const MAX_MOTION_BYTES: u64 = 64 * 1024 * 1024;
    let size = entry.size();
    if size > MAX_MOTION_BYTES {
        return None;
    }
    let mut limited = entry.take(size + 1);
    if extension == "bvh" {
        return validate_bvh(limited);
    }
    // An NPZ is itself a ZIP. Buffer only this member so its directory can be read.
    let mut bytes = Vec::new();
    if limited.read_to_end(&mut bytes).ok()? as u64 != size {
        return None;
    }
    validate_npz(Cursor::new(bytes)).ok()
}

fn validate_npz(file: impl std::io::Read + std::io::Seek) -> Result<(), &'static str> {
    let archive = validate_download_archive(file)?;
    validate_npz_arrays(archive).ok_or("云端返回的 NPZ 文件不完整或已损坏，请重试")
}

fn validate_npz_arrays<R: std::io::Read + std::io::Seek>(mut archive: zip::ZipArchive<R>) -> Option<()> {
    use std::io::Read;

    let mut found_array = false;
    for index in 0..archive.len() {
        let mut array = archive.by_index(index).ok()?;
        if !array.is_file() || !array.name().ok()?.ends_with(".npy") {
            continue;
        }
        let mut prefix = [0; 8];
        array.read_exact(&mut prefix).ok()?;
        if &prefix[..6] != b"\x93NUMPY" {
            return None;
        }
        let mut length = [0; 4];
        let width = match &prefix[6..] {
            [1, 0] => 2,
            [2 | 3, 0] => 4,
            _ => return None,
        };
        array.read_exact(&mut length[..width]).ok()?;
        let header_size = u32::from_le_bytes(length) as usize;
        if !(1..=10_000).contains(&header_size) {
            return None;
        }
        let mut header = vec![0; header_size];
        array.read_exact(&mut header).ok()?;
        if header.last() != Some(&b'\n') {
            return None;
        }
        found_array = true;
    }
    found_array.then_some(())
}

fn validate_motion_batch(mut archive: zip::ZipArchive<std::fs::File>, extension: &str, ids: &[String])
    -> Result<BatchSummary, &'static str>
{
    use std::collections::HashSet;
    use std::io::Read;

    const INVALID_REPORT: &str = "云端导出清单与所选动作或压缩包内容不一致，请重试";
    const REPORT_NAME: &str = "export-report.json";
    const MAX_REPORT_BYTES: u64 = 64 * 1024;
    if !ids.iter().any(|id| archive.by_name(&format!("{id}.{extension}")).ok()
        .is_some_and(|entry| entry.is_file() && entry.size() > 0))
    {
        return Err("批次未包含所选格式的动作文件，请尝试单独下载");
    }
    let report: BatchReport = {
        let mut entry = archive.by_name(REPORT_NAME).map_err(|_| INVALID_REPORT)?;
        if !entry.is_file() || entry.size() > MAX_REPORT_BYTES {
            return Err(INVALID_REPORT);
        }
        let mut json = Vec::new();
        entry.read_to_end(&mut json).map_err(|_| INVALID_REPORT)?;
        serde_json::from_slice(&json).map_err(|_| INVALID_REPORT)?
    };
    if report.format != extension || report.results.len() != ids.len() {
        return Err(INVALID_REPORT);
    }
    let mut expected: HashSet<_> = ids.iter().map(String::as_str).collect();
    let mut saved = 0;
    let mut failures = Vec::new();
    for row in report.results {
        if !expected.remove(row.motion_id.as_str()) {
            return Err(INVALID_REPORT);
        }
        let filename = format!("{}.{extension}", row.motion_id);
        match (row.status.as_str(), row.file.as_deref()) {
            ("saved", Some(file)) if file == filename => {
                let entry = archive.by_name(file).map_err(|_| INVALID_REPORT)?;
                if !entry.is_file() || entry.size() == 0 {
                    return Err(INVALID_REPORT);
                }
                if validate_batch_member(entry, extension).is_none() {
                    return Err("批次中的动作文件不完整或已损坏，请重试");
                }
                saved += 1;
            }
            ("failed", None) => failures.push(BatchFailure {
                motion_id: row.motion_id,
                error: batch_error_text(row.error.as_deref().unwrap_or_default()),
            }),
            _ => return Err(INVALID_REPORT),
        }
    }
    if saved == 0 || archive.len() != saved + 1 {
        return Err(INVALID_REPORT);
    }
    Ok(BatchSummary { saved, failed: failures.len(), failures })
}

#[derive(Deserialize)]
struct BatchReport {
    format: String,
    results: Vec<BatchEntry>,
}

#[derive(Deserialize)]
struct BatchEntry {
    motion_id: String,
    status: String,
    file: Option<String>,
    error: Option<String>,
}

#[derive(serde::Serialize)]
struct BatchFailure {
    motion_id: String,
    error: String,
}

#[derive(serde::Serialize)]
struct BatchSummary {
    saved: usize,
    failed: usize,
    failures: Vec<BatchFailure>,
}

fn batch_error_text(error: &str) -> String {
    error.trim().chars().take(240).collect()
}

fn validate_batch_error(data: &Value, ids: &[String]) -> Option<BatchSummary> {
    use std::collections::HashSet;

    let rows = data.get("failures")?.as_array()?;
    if !(1..=20).contains(&ids.len()) || rows.len() != ids.len() {
        return None;
    }
    let mut expected: HashSet<_> = ids.iter().map(String::as_str).collect();
    let mut failures = Vec::with_capacity(rows.len());
    for row in rows {
        let motion_id = row.get("motion_id")?.as_str()?;
        if row.get("status")?.as_str()? != "failed" || !expected.remove(motion_id) {
            return None;
        }
        failures.push(BatchFailure {
            motion_id: motion_id.to_owned(),
            error: batch_error_text(row.get("error")?.as_str()?),
        });
    }
    Some(BatchSummary { saved: 0, failed: failures.len(), failures })
}

fn read_bvh_offset<'a>(fields: &mut impl Iterator<Item = &'a str>) -> Option<()> {
    if fields.next()? != "OFFSET" {
        return None;
    }
    for _ in 0..3 {
        if !fields.next()?.parse::<f64>().ok()?.is_finite() {
            return None;
        }
    }
    Some(())
}

fn read_bvh_joint<'a>(
    fields: &mut impl Iterator<Item = &'a str>,
    names: &mut std::collections::HashSet<&'a str>,
) -> Option<usize> {
    let name = fields.next()?;
    if matches!(name, "{" | "}") || !names.insert(name) || fields.next()? != "{" {
        return None;
    }
    read_bvh_offset(fields)?;
    if fields.next()? != "CHANNELS" {
        return None;
    }
    let count = fields.next()?.parse::<usize>().ok()?;
    let valid = ["Xposition", "Yposition", "Zposition", "Xrotation", "Yrotation", "Zrotation"];
    let mut used = [false; 6];
    for _ in 0..count {
        let channel = fields.next()?;
        let index = valid.iter().position(|candidate| *candidate == channel)?;
        if used[index] {
            return None;
        }
        used[index] = true;
    }
    Some(count)
}

fn bvh_channel_count(hierarchy: &str) -> Option<usize> {
    let tokens = hierarchy.replace('{', " { ").replace('}', " } ");
    let mut fields = tokens.split_whitespace();
    if fields.next()? != "HIERARCHY" || fields.next()? != "ROOT" {
        return None;
    }
    let mut names = std::collections::HashSet::new();
    let mut channels = read_bvh_joint(&mut fields, &mut names)?;
    let mut depth = 1usize;
    while depth > 0 {
        match fields.next()? {
            "JOINT" => {
                channels = channels.checked_add(read_bvh_joint(&mut fields, &mut names)?)?;
                depth += 1;
            }
            "End" => {
                if fields.next()? != "Site" || fields.next()? != "{" {
                    return None;
                }
                read_bvh_offset(&mut fields)?;
                if fields.next()? != "}" {
                    return None;
                }
            }
            "}" => depth -= 1,
            _ => return None,
        }
    }
    (fields.next().is_none() && channels > 0).then_some(channels)
}

fn validate_bvh(file: impl std::io::Read) -> Option<()> {
    use std::io::{BufRead, BufReader};

    let mut lines = BufReader::new(file).lines()
        .filter(|line| !line.as_ref().is_ok_and(|line| line.trim().is_empty()));
    let mut hierarchy = String::new();
    loop {
        let line = lines.next()?.ok()?;
        if line.trim() == "MOTION" {
            break;
        }
        hierarchy.push_str(&line);
        hierarchy.push('\n');
    }
    let channels = bvh_channel_count(&hierarchy)?;

    let frames = lines.next()?.ok()?.trim().strip_prefix("Frames:")?
        .trim().parse::<usize>().ok()?;
    let frame_time = lines.next()?.ok()?.trim().strip_prefix("Frame Time:")?
        .trim().parse::<f64>().ok()?;
    if frames == 0 || !frame_time.is_finite() || frame_time <= 0.0 {
        return None;
    }

    let mut actual = 0usize;
    for line in lines {
        let line = line.ok()?;
        actual += 1;
        let mut fields = line.split_whitespace();
        if actual > frames || fields.clone().count() != channels
            || !fields.all(|value| value.parse::<f64>().is_ok_and(f64::is_finite))
        {
            return None;
        }
    }
    (actual == frames).then_some(())
}

const DOWNLOAD_WRITE_ERROR: &str = "无法写入所选位置";
const DOWNLOAD_DESTINATION_CHANGED: &str = "所选文件在下载期间发生变化，请重新选择保存位置";
const DOWNLOAD_DIRECTORY_CHANGED: &str = "所选文件夹在下载期间发生变化，请重新选择保存位置";

#[cfg(any(target_os = "linux", target_os = "macos"))]
use download_staging::DownloadStaging;
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
type DownloadStaging = tempfile::TempPath;

struct DownloadDestination {
    directory: std::fs::Metadata,
    file: Option<std::fs::Metadata>,
}

impl DownloadDestination {
    fn capture(path: &Path) -> Result<Self, &'static str> {
        let directory = std::fs::metadata(Self::parent(path)).map_err(|_| DOWNLOAD_WRITE_ERROR)?;
        let file = Self::file_at(path)?;
        Ok(Self { directory, file })
    }

    fn parent(path: &Path) -> &Path {
        path.parent().filter(|dir| !dir.as_os_str().is_empty()).unwrap_or(Path::new("."))
    }

    fn file_at(path: &Path) -> Result<Option<std::fs::Metadata>, &'static str> {
        match std::fs::symlink_metadata(path) {
            Ok(metadata) => Ok(Some(metadata)),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(_) => Err(DOWNLOAD_WRITE_ERROR),
        }
    }

    fn verify_directory(&self, path: &Path) -> Result<(), &'static str> {
        let current = std::fs::metadata(Self::parent(path)).map_err(|_| DOWNLOAD_DIRECTORY_CHANGED)?;
        if !self.is_same_directory(&current) {
            return Err(DOWNLOAD_DIRECTORY_CHANGED);
        }
        Ok(())
    }

    fn is_same_directory(&self, current: &std::fs::Metadata) -> bool {
        #[cfg(unix)]
        let same_directory = {
            use std::os::unix::fs::MetadataExt;
            (self.directory.dev(), self.directory.ino()) == (current.dev(), current.ino())
        };
        #[cfg(not(unix))]
        let same_directory = self.directory.created().ok() == current.created().ok();
        current.is_dir() && same_directory
    }

    fn stage(&self, path: &Path) -> Result<(std::fs::File, DownloadStaging), &'static str> {
        self.verify_directory(path)?;
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        return DownloadStaging::create(Self::parent(path), self);

        #[cfg(not(any(target_os = "linux", target_os = "macos")))]
        return tempfile::NamedTempFile::new_in(Self::parent(path))
            .map(tempfile::NamedTempFile::into_parts)
            .map_err(|_| DOWNLOAD_WRITE_ERROR);
    }

    fn persist(self, staging: DownloadStaging, path: PathBuf) -> Result<(), &'static str> {
        self.verify_directory(&path)?;
        match self.file {
            None => staging.persist_noclobber(path).map_err(|error| {
                let error: std::io::Error = error.into();
                if error.kind() == std::io::ErrorKind::AlreadyExists {
                    DOWNLOAD_DESTINATION_CHANGED
                } else {
                    DOWNLOAD_WRITE_ERROR
                }
            }),
            Some(expected) => {
                match Self::file_at(&path)? {
                    Some(current) if Self::matches(&expected, &current) => (),
                    _ => return Err(DOWNLOAD_DESTINATION_CHANGED),
                }
                staging.persist(path).map_err(|_| DOWNLOAD_WRITE_ERROR)
            }
        }
    }

    fn matches(expected: &std::fs::Metadata, current: &std::fs::Metadata) -> bool {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if (expected.dev(), expected.ino(), expected.ctime(), expected.ctime_nsec())
                != (current.dev(), current.ino(), current.ctime(), current.ctime_nsec())
            {
                return false;
            }
        }
        expected.file_type() == current.file_type() && expected.len() == current.len()
            && expected.modified().ok() == current.modified().ok()
    }
}

async fn write_download(
    path: &Path,
    mut response: reqwest::Response,
    format: DownloadFormat,
    destination: DownloadDestination,
    commit: impl FnOnce(DownloadDestination, DownloadStaging, PathBuf) -> Result<(), &'static str> + Send + 'static,
) -> Result<(u64, Option<BatchSummary>), &'static str> {
    use std::io::{Seek, SeekFrom};
    use tokio::io::AsyncWriteExt;
    // This download sends no Range header and requires the complete artifact.
    if response.status() != reqwest::StatusCode::OK {
        return Err("云端未返回完整文件，请重试");
    }
    if let Some(content_type) = response.headers().get(reqwest::header::CONTENT_TYPE).and_then(|value| value.to_str().ok()) {
        let media_type = content_type.split(';').next().unwrap_or_default().trim().to_ascii_lowercase();
        if matches!(media_type.as_str(), "text/html" | "application/xhtml+xml" | "application/json" | "text/json")
            || media_type.ends_with("+json")
        {
            return Err("云端返回了网页或错误信息，无法保存文件");
        }
    }
    // Stage on the same filesystem so the selected destination is replaced atomically.
    let path = path.to_owned();
    let (path, destination, file, staging) = tauri::async_runtime::spawn_blocking(move || {
        let (file, staging) = destination.stage(&path)?;
        Ok::<_, &'static str>((path, destination, file, staging))
    }).await.map_err(|_| DOWNLOAD_WRITE_ERROR)??;
    let mut file = tokio::fs::File::from_std(file);
    let mut bytes = 0;
    let mut header = [0; b"HIERARCHY ".len()];
    let mut header_len = 0;
    while let Some(chunk) = response.chunk().await.map_err(|_| "下载中断，请重试")? {
        let prefix_len = chunk.len().min(header.len() - header_len);
        header[header_len..header_len + prefix_len].copy_from_slice(&chunk[..prefix_len]);
        header_len += prefix_len;
        file.write_all(&chunk).await.map_err(|_| DOWNLOAD_WRITE_ERROR)?;
        bytes += chunk.len() as u64;
    }
    if bytes == 0 {
        return Err("云端返回了空文件，请重试");
    }
    if !format.matches_header(&header[..header_len]) {
        return Err("云端返回的内容与下载格式不符，无法保存文件");
    }
    file.flush().await.map_err(|_| DOWNLOAD_WRITE_ERROR)?;
    let mut downloaded = file.into_std().await;

    let batch = tauri::async_runtime::spawn_blocking(move || {
        downloaded.seek(SeekFrom::Start(0)).map_err(|_| DOWNLOAD_WRITE_ERROR)?;
        let batch = match format {
            DownloadFormat::Archive => {
                validate_download_archive(downloaded)?;
                None
            }
            DownloadFormat::Snapshot { id, batch } => {
                validate_snapshot_batch(validate_download_archive(downloaded)?, &id, batch)?;
                None
            }
            DownloadFormat::MotionBatch { extension, ids } => {
                Some(validate_motion_batch(validate_download_archive(downloaded)?, extension, &ids)?)
            }
            DownloadFormat::Bvh => {
                if validate_bvh(downloaded).is_none() {
                    return Err("云端返回的 BVH 文件不完整或已损坏，请重试");
                }
                None
            }
            DownloadFormat::Npz => {
                validate_npz(downloaded)?;
                None
            }
        };
        commit(destination, staging, path)?;
        Ok(batch)
    })
    .await.map_err(|_| DOWNLOAD_WRITE_ERROR)??;
    Ok((bytes, batch))
}

#[cfg(test)]
mod download_tests {
    use super::{error_body, server, write_download, Cloud, DownloadDestination, DownloadFormat, DownloadStaging};
    use serde_json::json;
    use std::{
        fs,
        io::{BufRead, BufReader, Cursor, Write},
        net::{TcpListener, TcpStream},
        path::PathBuf,
        sync::{atomic::AtomicBool, RwLock},
        time::{Duration, Instant},
    };

    fn serve(length: usize, body: impl FnOnce(TcpStream) + Send + 'static) -> (String, std::thread::JoinHandle<()>) {
        serve_status("200 OK", length, body)
    }

    fn serve_status(status: &'static str, length: usize, body: impl FnOnce(TcpStream) + Send + 'static) -> (String, std::thread::JoinHandle<()>) {
        serve_response(status, length, "", body)
    }

    fn serve_response(status: &'static str, length: usize, headers: &'static str, body: impl FnOnce(TcpStream) + Send + 'static) -> (String, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let thread = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            for line in BufReader::new(&stream).lines() {
                if line.unwrap().is_empty() { break; }
            }
            write!(stream, "HTTP/1.1 {status}\r\nContent-Length: {length}\r\nConnection: close\r\n{headers}\r\n").unwrap();
            body(stream);
        });
        (url, thread)
    }

    fn save(path: &std::path::Path, url: String) -> Result<u64, &'static str> {
        save_as(path, url, DownloadFormat::Archive)
    }

    fn test_archive() -> Vec<u8> {
        let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Stored);
        archive.start_file("motion.npy", options).unwrap();
        archive.write_all(b"new export").unwrap();
        archive.finish().unwrap().into_inner()
    }

    fn test_npz() -> Vec<u8> {
        let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
        archive.start_file("motion.npy", zip::write::SimpleFileOptions::default()).unwrap();
        let mut header = b"{'descr': '|u1', 'fortran_order': False, 'shape': (1,), }".to_vec();
        header.resize(117, b' ');
        header.push(b'\n');
        archive.write_all(b"\x93NUMPY\x01\x00").unwrap();
        archive.write_all(&(header.len() as u16).to_le_bytes()).unwrap();
        archive.write_all(&header).unwrap();
        archive.write_all(&[42]).unwrap();
        archive.finish().unwrap().into_inner()
    }

    fn test_snapshot(id: &str, batch: usize, incorrect_hash: bool, missing_member: bool) -> Vec<u8> {
        use sha2::{Digest, Sha256};

        let payload = test_npz();
        let digest = if incorrect_hash { "0".repeat(64) } else { format!("{:x}", Sha256::digest(&payload)) };
        let manifest = json!({"schema":1,"id":id,"items":[
            {"motion_id":"m00000001","bytes":payload.len(),"sha256":digest},
            {"motion_id":"m00000002","bytes":payload.len(),"sha256":digest}
        ],"batches":[[0,1],[1,2]]});
        let details = json!({"batch":batch,"batches":2,"start":batch-1,"end":batch});
        let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let options = zip::write::SimpleFileOptions::default();
        archive.start_file("manifest.json", options).unwrap();
        archive.write_all(manifest.to_string().as_bytes()).unwrap();
        archive.start_file("batch.json", options).unwrap();
        archive.write_all(details.to_string().as_bytes()).unwrap();
        if !missing_member {
            archive.start_file(format!("motions/m{batch:08}.npz"), options).unwrap();
            archive.write_all(&payload).unwrap();
        }
        archive.finish().unwrap().into_inner()
    }

    #[test]
    fn snapshot_download_validates_identity_batch_and_member_bytes_before_replacing_file() {
        const ID: &str = "0123456789abcdef0123456789abcdef";
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("snapshot.zip");
        fs::write(&target, b"previous snapshot").unwrap();
        let path = format!("/api/harness/personal-assets/snapshots/{ID}/download");
        let format = DownloadFormat::for_request(&path, Some(&json!({"batch":2})));
        for body in [
            test_snapshot("fedcba9876543210fedcba9876543210", 2, false, false),
            test_snapshot(ID, 1, false, false),
            test_snapshot(ID, 2, true, false),
            test_snapshot(ID, 2, false, true),
            test_archive(),
        ] {
            let (url, server) = serve(body.len(), move |mut stream| stream.write_all(&body).unwrap());
            let result = save_as(&target, url, format.clone());
            server.join().unwrap();
            assert!(result.is_err(), "saved a different or incomplete snapshot");
            assert_eq!(fs::read(&target).unwrap(), b"previous snapshot");
            assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
        }
        let valid = test_snapshot(ID, 2, false, false);
        let payload = valid.clone();
        let (url, server) = serve(payload.len(), move |mut stream| stream.write_all(&payload).unwrap());
        assert_eq!(save_as(&target, url, format), Ok(valid.len() as u64));
        server.join().unwrap();
        assert_eq!(fs::read(&target).unwrap(), valid);
    }

    #[test]
    fn snapshot_download_allows_hyphenated_motion_ids() {
        use sha2::{Digest, Sha256};

        const ID: &str = "0123456789abcdef0123456789abcdef";
        let payload = test_npz();
        let digest = format!("{:x}", Sha256::digest(&payload));
        let motion_id = "m000096vn-semantic";
        let manifest = json!({"schema":1,"id":ID,"items":[
            {"motion_id":motion_id,"bytes":payload.len(),"sha256":digest}
        ],"batches":[[0,1]]});
        let details = json!({"batch":1,"batches":1,"start":0,"end":1});
        let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let options = zip::write::SimpleFileOptions::default();
        archive.start_file("manifest.json", options).unwrap();
        archive.write_all(manifest.to_string().as_bytes()).unwrap();
        archive.start_file("batch.json", options).unwrap();
        archive.write_all(details.to_string().as_bytes()).unwrap();
        archive.start_file(format!("motions/{motion_id}.npz"), options).unwrap();
        archive.write_all(&payload).unwrap();
        let valid = archive.finish().unwrap().into_inner();

        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("snapshot-hyphen.zip");
        let path = format!("/api/harness/personal-assets/snapshots/{ID}/download");
        let format = DownloadFormat::for_request(&path, Some(&json!({"batch":1})));
        let body = valid.clone();
        let (url, server) = serve(body.len(), move |mut stream| stream.write_all(&body).unwrap());
        assert_eq!(save_as(&target, url, format), Ok(valid.len() as u64));
        server.join().unwrap();
        assert_eq!(fs::read(&target).unwrap(), valid);
    }

    #[test]
    fn single_motion_download_rejects_a_zip_without_readable_numpy_arrays() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("motion.npz");
        fs::write(&target, b"existing motion").unwrap();
        let path = "/api/harness/motions/ma_0123456789abcdef0123456789abcdef/download";
        for query in [None, Some(json!({"format":"npz"}))] {
            let format = DownloadFormat::for_request(path, query.as_ref());
            for body in [batch_archive(&[]), test_archive()] {
                let (url, server) = serve(body.len(), move |mut conn| conn.write_all(&body).unwrap());
                let result = save_as(&target, url, format.clone());
                server.join().unwrap();
                assert_eq!(result, Err("云端返回的 NPZ 文件不完整或已损坏，请重试"));
                assert_eq!(fs::read(&target).unwrap(), b"existing motion");
                assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
            }
        }
        let valid = test_npz();
        let body = valid.clone();
        let (url, server) = serve(body.len(), move |mut conn| conn.write_all(&body).unwrap());
        assert_eq!(save_as(&target, url, DownloadFormat::for_request(path, None)), Ok(valid.len() as u64));
        server.join().unwrap();
        assert_eq!(fs::read(&target).unwrap(), valid);
    }

    #[test]
    fn batch_rejects_unreadable_members_before_replacing_a_download() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("motions.zip");
        fs::write(&target, b"previous export").unwrap();
        let npz = test_npz();
        for (extension, payload) in [
            ("npz", b"not an NPZ".as_slice()),
            ("npz", &npz[..npz.len()-22]),
            ("npz", &test_archive()),
            ("bvh", b"HIERARCHY\nROOT Broken\nMOTION\nFrames: 1\nFrame Time: 0.033333\n".as_slice()),
        ] {
            let body = batch_archive(&[(extension, payload)]);
            let format = DownloadFormat::for_request("/api/harness/export",
                Some(&json!({"ids":"available,missing","format":extension})));
            let (url, server) = serve(body.len(), move |mut conn| conn.write_all(&body).unwrap());
            let result = save_as(&target, url, format);
            server.join().unwrap();
            assert_eq!(result, Err("批次中的动作文件不完整或已损坏，请重试"));
            assert_eq!(fs::read(&target).unwrap(), b"previous export");
            assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
        }
    }

    #[test]
    fn batch_export_keeps_the_previous_file_if_no_motion_was_exported() {
        for (format, other_format) in [("npz", "bvh"), ("bvh", "npz")] {
            let directory = tempfile::tempdir().unwrap();
            let target = directory.path().join("motions.zip");
            fs::write(&target, b"previous export").unwrap();
            let query = serde_json::json!({"ids": "available,missing", "format": format});
            let expected_format = DownloadFormat::for_request("/api/harness/export", Some(&query));
            for entries in [vec![], vec![(format, b"".as_slice())], vec![(other_format, b"wrong format".as_slice())]] {
                let body = batch_archive(&entries);
                let (url, server) = serve(body.len(), move |mut conn| conn.write_all(&body).unwrap());
                let result = save_as(&target, url, expected_format.clone());
                server.join().unwrap();
                assert_eq!(result, Err("批次未包含所选格式的动作文件，请尝试单独下载"));
                assert_eq!(fs::read(&target).unwrap(), b"previous export");
                assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
            }
            let motion = if format == "npz" { test_npz() } else {
                b"HIERARCHY\nROOT Base\n{\nOFFSET 0 0 0\nCHANNELS 3 Zrotation Xrotation Yrotation\n}\nMOTION\nFrames: 1\nFrame Time: 0.033333\n0 0 0\n".to_vec()
            };
            let bad_report = json!({"format":format,"results":[
                {"motion_id":"available","status":"saved","file":format!("available.{format}")},
                {"motion_id":"different","status":"failed"}
            ]}).to_string();
            let body = batch_archive_with_report(&[(format, &motion)], &bad_report);
            let (url, server) = serve(body.len(), move |mut conn| conn.write_all(&body).unwrap());
            assert!(save_as(&target, url, expected_format.clone()).is_err());
            server.join().unwrap();
            assert_eq!(fs::read(&target).unwrap(), b"previous export");

            let expected = batch_archive(&[(format, &motion)]);
            let body = expected.clone();
            let (url, server) = serve(body.len(), move |mut conn| conn.write_all(&body).unwrap());
            assert_eq!(save_as(&target, url, expected_format), Ok(expected.len() as u64));
            server.join().unwrap();
            assert_eq!(fs::read(&target).unwrap(), expected);
        }
    }

    #[test]
    fn validated_batch_reports_the_failed_motion_and_preserves_legacy_rows() {
        let ids = ["available", "missing", "older"].map(String::from);
        let report = json!({"format":"npz","results":[
            {"motion_id":"available","status":"saved","file":"available.npz"},
            {"motion_id":"missing","status":"failed","error":" 动作文件缺失 "},
            {"motion_id":"older","status":"failed"}
        ]});
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("motions.zip");
        fs::write(&path, batch_archive_with_report(&[("npz", &test_npz())], &report.to_string())).unwrap();
        let archive = super::validate_download_archive(fs::File::open(path).unwrap()).unwrap();
        let summary = super::validate_motion_batch(archive, "npz", &ids).unwrap();
        assert_eq!(summary.saved, 1);
        assert_eq!(summary.failed, 2);
        assert_eq!(summary.failures[0].motion_id, "missing");
        assert_eq!(summary.failures[0].error, "动作文件缺失");
        assert_eq!(summary.failures[1].motion_id, "older");
        assert!(summary.failures[1].error.is_empty());
    }

    #[test]
    fn failed_batch_response_only_keeps_details_for_the_current_selection() {
        let ids = ["large", "missing"].map(String::from);
        let report = json!({"error":"没有可导出的动作","failures":[
            {"motion_id":"large","status":"failed","error":"超出限制"},
            {"motion_id":"missing","status":"failed","error":"文件缺失"}
        ]});
        let fetch = |report: serde_json::Value, current: bool| {
            let body = report.to_string();
            let (url, server) = serve_status("422 Unprocessable Content", body.len(), move |mut conn| {
                conn.write_all(body.as_bytes()).unwrap();
            });
            let result = tauri::async_runtime::block_on(async {
                let response = reqwest::Client::builder().no_proxy().build().unwrap()
                    .get(url).timeout(Duration::from_secs(8)).send().await.unwrap();
                super::session_error(response, Some(&ids), || current).await
            });
            server.join().unwrap();
            result
        };
        let result = fetch(report.clone(), true);
        assert_eq!(result["error"], "没有可导出的动作");
        assert_eq!(result["batch"]["saved"], 0);
        assert_eq!(result["batch"]["failed"], 2);
        assert_eq!(result["batch"]["failures"][1]["error"], "文件缺失");
        assert!(fetch(report.clone(), false).get("batch").is_none());
        let mut invalid = report;
        invalid["failures"][1]["motion_id"] = json!("large");
        assert!(fetch(invalid, true).get("batch").is_none());
    }

    fn batch_archive(entries: &[(&str, &[u8])]) -> Vec<u8> {
        let extension = entries.first().map_or("npz", |&(extension, _)| extension);
        let report = json!({"format":extension,"results":[
            {"motion_id":"available","status":"saved","file":format!("available.{extension}")},
            {"motion_id":"missing","status":"failed"}
        ]}).to_string();
        batch_archive_with_report(entries, &report)
    }

    fn batch_archive_with_report(entries: &[(&str, &[u8])], report: &str) -> Vec<u8> {
        let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let options = zip::write::SimpleFileOptions::default();
        archive.start_file("export-report.json", options).unwrap();
        archive.write_all(report.as_bytes()).unwrap();
        for (extension, payload) in entries {
            archive.start_file(format!("available.{extension}"), options).unwrap();
            archive.write_all(payload).unwrap();
        }
        archive.finish().unwrap().into_inner()
    }

    fn save_as(path: &std::path::Path, url: String, format: DownloadFormat) -> Result<u64, &'static str> {
        save_with_commit(path, url, format, DownloadDestination::persist)
    }

    fn save_with_commit(
        path: &std::path::Path,
        url: String,
        format: DownloadFormat,
        commit: impl FnOnce(DownloadDestination, DownloadStaging, PathBuf) -> Result<(), &'static str> + Send + 'static,
    ) -> Result<u64, &'static str> {
        let destination = DownloadDestination::capture(path)?;
        tauri::async_runtime::block_on(async {
            let response = reqwest::Client::builder().no_proxy().build().unwrap()
                .get(url).timeout(Duration::from_secs(8)).send().await.unwrap();
            write_download(path, response, format, destination, commit).await.map(|(bytes, _)| bytes)
        })
    }

    #[test]
    fn blank_http_error_message_falls_back_to_status() {
        for body in [br#"{"error":""}"#.as_slice(), br#"{"error":"   "}"#] {
            let (url, server) = serve_status("503 Service Unavailable", body.len(), move |mut stream| {
                stream.write_all(body).unwrap();
            });
            let failure = tauri::async_runtime::block_on(async {
                let response = reqwest::Client::builder().no_proxy().build().unwrap()
                    .get(url).timeout(Duration::from_secs(8)).send().await.unwrap();
                error_body(response, None).await
            });
            server.join().unwrap();
            assert_eq!(failure["error"], "请求失败（503）");
        }
    }

    #[test]
    fn pending_download_is_rejected_after_logout_relogin_or_server_change() {
        let folder = tempfile::tempdir().unwrap();
        let origin = "http://localhost:8000";
        let connection = server::connection(folder.path(), origin.into());
        connection.session.apply(["mf_session=first-test-login; Max-Age=60"].into_iter());
        let cloud = Cloud {
            client: reqwest::Client::new(),
            connection: RwLock::new(Some(connection.clone())),
            directory: folder.path().to_owned(),
            preference_dir: None,
            preference_unavailable: AtomicBool::new(false),
        };
        let initial_cookie = connection.session.cookie_header();
        let prepare = || cloud.prepare_download(&connection, initial_cookie.as_deref(), origin);
        let request = prepare().unwrap().build().unwrap();
        assert_eq!(request.headers()["Cookie"], "mf_session=first-test-login");

        connection.session.clear();
        assert!(prepare().is_err());
        connection.session.apply(["mf_session=second-test-login; Max-Age=60"].into_iter());
        assert!(prepare().is_err());

        // Replacing the connection also invalidates a picker opened under an old connection.
        connection.session.apply(["mf_session=first-test-login; Max-Age=60"].into_iter());
        *cloud.connection.write().unwrap() = Some(server::connection(folder.path(), origin.into()));
        assert!(prepare().is_err());
    }

    #[test]
    fn slow_download_does_not_replace_a_file_after_sign_out_or_server_switch() {
        for switch_server in [false, true] {
            let folder = tempfile::tempdir().unwrap();
            let target = folder.path().join("motion.npz");
            fs::write(&target, b"previous export").unwrap();

            let session_dir = tempfile::tempdir().unwrap();
            let origin = "http://localhost:8000";
            let connection = server::connection(session_dir.path(), origin.into());
            connection.session.apply(["mf_session=test-download; Max-Age=60"].into_iter());
            let cloud = std::sync::Arc::new(Cloud {
                client: reqwest::Client::new(),
                connection: RwLock::new(Some(connection.clone())),
                directory: session_dir.path().to_owned(),
                preference_dir: None,
                preference_unavailable: AtomicBool::new(false),
            });
            let directory = folder.path().to_owned();
            let original = target.clone();
            let active = cloud.clone();
            let login = connection.clone();
            let cookie = connection.session.cookie_header();
            let body = test_archive();
            let (url, server) = serve(body.len(), move |mut stream| {
                stream.write_all(&body[..4]).unwrap();
                wait_for_staging(directory, original);
                if switch_server {
                    *active.connection.write().unwrap() =
                        Some(server::connection(&active.directory, "http://localhost:9000".into()));
                } else {
                    login.session.clear();
                }
                stream.write_all(&body[4..]).unwrap();
            });

            let result = save_with_commit(&target, url, DownloadFormat::Archive, move |destination, staging, path| {
                cloud.with_download_session(&connection, cookie.as_deref(), || destination.persist(staging, path))?
            });
            server.join().unwrap();
            assert_eq!(result, Err("下载会话已变更，请重试"));
            assert_eq!(fs::read(&target).unwrap(), b"previous export");
            assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
        }
    }

    #[test]
    fn completed_download_preserves_a_destination_changed_during_transfer() {
        for exists_at_selection in [false, true] {
            let folder = tempfile::tempdir().unwrap();
            let target = folder.path().join("selected-motion.npz");
            if exists_at_selection {
                fs::write(&target, b"previous export").unwrap();
            }
            let changed = target.clone();
            let body = test_archive();
            let (url, server) = serve(body.len(), move |mut stream| {
                fs::write(&changed, b"an independent edit during the download").unwrap();
                stream.write_all(&body).unwrap();
            });
            let result = save(&target, url);
            server.join().unwrap();
            assert_eq!(result, Err("所选文件在下载期间发生变化，请重新选择保存位置"));
            assert_eq!(fs::read(&target).unwrap(), b"an independent edit during the download");
            assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
        }
    }

    #[test]
    fn replaced_destination_folder_requires_selecting_a_location_again() {
        let folder = tempfile::tempdir().unwrap();
        let selected = folder.path().join("chosen");
        let moved = folder.path().join("original");
        fs::create_dir(&selected).unwrap();
        let target = selected.join("motion.npz");
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let body = test_archive();
        let new_folder = selected.clone();
        let original_folder = moved.clone();
        let sender = std::thread::spawn(move || {
            let (mut conn, _) = listener.accept().unwrap();
            conn.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            for line in BufReader::new(&conn).lines() {
                if line.unwrap().is_empty() { break; }
            }
            fs::rename(&new_folder, &original_folder).unwrap();
            fs::create_dir(&new_folder).unwrap();
            write!(conn, "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).unwrap();
            conn.write_all(&body).unwrap();
        });
        let result = save(&target, url);
        sender.join().unwrap();
        assert_eq!(result, Err("所选文件夹在下载期间发生变化，请重新选择保存位置"));
        assert_eq!(fs::read_dir(&selected).unwrap().count(), 0);
        assert_eq!(fs::read_dir(&moved).unwrap().count(), 0);

        let expected = test_archive();
        let retry = expected.clone();
        let (url, sender) = serve(retry.len(), move |mut stream| stream.write_all(&retry).unwrap());
        assert_eq!(save(&target, url), Ok(expected.len() as u64));
        sender.join().unwrap();
        assert_eq!(fs::read(target).unwrap(), expected);
    }

    fn wait_for_staging(folder: PathBuf, target: PathBuf) {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if fs::read_dir(&folder).unwrap().filter_map(Result::ok)
                .any(|entry| entry.path() != target && entry.metadata().unwrap().len() > 0) {
                break;
            }
            assert!(Instant::now() < deadline, "Response was not staged before the transfer finished");
            std::thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(fs::read(&target).unwrap(), b"previous export");
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    #[test]
    fn moving_destination_during_transfer_cleans_up_the_staged_file() {
        for truncated in [false, true] {
            let folder = tempfile::tempdir().unwrap();
            let selected = folder.path().join("selected");
            let moved = folder.path().join("moved");
            fs::create_dir(&selected).unwrap();
            let target = selected.join("motion.npz");
            fs::write(&target, b"previous export").unwrap();
            let body = test_archive();
            let selected_dir = selected.clone();
            let moved_dir = moved.clone();
            let selected_file = target.clone();
            let (url, sender) = serve(body.len(), move |mut conn| {
                conn.write_all(&body[..4]).unwrap();
                wait_for_staging(selected_dir.clone(), selected_file);
                fs::rename(&selected_dir, &moved_dir).unwrap();
                fs::create_dir(&selected_dir).unwrap();
                if !truncated { conn.write_all(&body[4..]).unwrap(); }
            });

            let result = save(&target, url);
            sender.join().unwrap();
            assert_eq!(fs::read_dir(&moved).unwrap().count(), 1, "left a partial export in the moved directory");
            assert_eq!(fs::read(moved.join("motion.npz")).unwrap(), b"previous export");
            assert_eq!(fs::read_dir(&selected).unwrap().count(), 0);
            assert_eq!(result, Err(if truncated { "下载中断，请重试" } else { super::DOWNLOAD_DIRECTORY_CHANGED }));
        }
    }

    #[test]
    fn save_replaces_only_the_selected_file() {
        let folder = tempfile::tempdir().unwrap();
        let path = folder.path().join("selected-motion.npz");
        let other = path.with_extension("kinora.part");
        fs::write(&path, b"previous export").unwrap();
        fs::write(&other, b"unrelated file").unwrap();

        let expected = test_archive();
        let body = expected.clone();
        let (url, server) = serve(body.len(), move |mut stream| stream.write_all(&body).unwrap());
        assert_eq!(save(&path, url).unwrap(), expected.len() as u64);
        server.join().unwrap();

        assert_eq!(fs::read(&path).unwrap(), expected);
        assert_eq!(fs::read(&other).unwrap(), b"unrelated file");
        assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 2);
    }

    #[test]
    fn failed_save_preserves_existing_files_and_cleans_up() {
        let folder = tempfile::tempdir().unwrap();
        let path = folder.path().join("selected-motion.npz");
        let other = path.with_extension("kinora.part");
        fs::create_dir(&path).unwrap();
        fs::write(&other, b"unrelated file").unwrap();

        let body = test_archive();
        let (url, server) = serve(body.len(), move |mut stream| stream.write_all(&body).unwrap());
        assert!(save(&path, url).is_err());
        server.join().unwrap();

        assert!(path.is_dir());
        assert_eq!(fs::read(&other).unwrap(), b"unrelated file");
        assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 2);
    }

    #[test]
    fn transfer_is_staged_incrementally_and_a_truncated_body_keeps_the_old_file() {
        for truncated in [false, true] {
            let folder = tempfile::tempdir().unwrap();
            let target = folder.path().join("motion.npz");
            fs::write(&target, b"previous export").unwrap();
            let directory = folder.path().to_owned();
            let original = target.clone();
            let expected = test_archive();
            let body = expected.clone();
            let (url, server) = serve(body.len(), move |mut stream| {
                stream.write_all(&body[..4]).unwrap();
                wait_for_staging(directory, original);
                if !truncated { stream.write_all(&body[4..]).unwrap(); }
            });
            let result = save(&target, url);
            server.join().unwrap();
            if truncated {
                assert_eq!(result, Err("下载中断，请重试"));
                assert_eq!(fs::read(&target).unwrap(), b"previous export");
            } else {
                assert_eq!(result, Ok(expected.len() as u64));
                assert_eq!(fs::read(&target).unwrap(), expected);
            }
            assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
        }
    }

    #[test]
    fn rejects_empty_and_partial_success_responses_without_replacing_old_file() {
        for (status, body, expected) in [
            ("200 OK", b"".as_slice(), "云端返回了空文件，请重试"),
            ("204 No Content", b"".as_slice(), "云端未返回完整文件，请重试"),
            ("206 Partial Content", b"part".as_slice(), "云端未返回完整文件，请重试"),
        ] {
            let folder = tempfile::tempdir().unwrap();
            let target = folder.path().join("motion.npz");
            fs::write(&target, b"previous export").unwrap();
            let (url, server) = serve_status(status, body.len(), move |mut stream| stream.write_all(body).unwrap());
            let result = save(&target, url);
            server.join().unwrap();
            assert_eq!(result, Err(expected), "{status}");
            assert_eq!(fs::read(&target).unwrap(), b"previous export");
            assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
        }
    }

    #[test]
    fn refuses_proxy_documents_without_replacing_old_file() {
        for (headers, body) in [
            ("Content-Type: Text/HTML; charset=UTF-8\r\n", b"<html>Sign in</html>".as_slice()),
            ("Content-Type: application/problem+json; charset=utf-8\r\n", br#"{"detail":"Gateway error"}"#.as_slice()),
        ] {
            let folder = tempfile::tempdir().unwrap();
            let target = folder.path().join("motion.npz");
            fs::write(&target, b"previous export").unwrap();
            let (url, server) = serve_response("200 OK", body.len(), headers, move |mut stream| {
                stream.write_all(body).unwrap();
            });
            let result = save(&target, url);
            server.join().unwrap();
            assert_eq!(result, Err("云端返回了网页或错误信息，无法保存文件"));
            assert_eq!(fs::read(&target).unwrap(), b"previous export");
            assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
        }
    }

    #[test]
    fn a_success_response_with_an_error_body_does_not_replace_the_archive() {
        for headers in ["Content-Type: text/plain\r\n", "Content-Type: application/octet-stream\r\n", ""] {
            let folder = tempfile::tempdir().unwrap();
            let target = folder.path().join("motion.npz");
            fs::write(&target, b"previous export").unwrap();
            let body = b"Upstream service unavailable";
            let (url, server) = serve_response("200 OK", body.len(), headers, move |mut stream| {
                stream.write_all(body).unwrap();
            });
            let result = save(&target, url);
            server.join().unwrap();
            assert_eq!(result, Err("云端返回的内容与下载格式不符，无法保存文件"), "{headers:?}");
            assert_eq!(fs::read(&target).unwrap(), b"previous export");
            assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
        }
    }

    #[test]
    fn a_complete_http_transfer_with_an_incomplete_archive_keeps_the_old_file() {
        let folder = tempfile::tempdir().unwrap();
        let target = folder.path().join("motion.npz");
        fs::write(&target, b"previous export").unwrap();
        let mut body = test_archive();
        body.truncate(body.len() - 12);
        let (url, server) = serve(body.len(), move |mut stream| stream.write_all(&body).unwrap());
        let result = save(&target, url);
        server.join().unwrap();
        assert_eq!(result, Err("云端返回的压缩文件不完整或已损坏，请重试"));
        assert_eq!(fs::read(&target).unwrap(), b"previous export");
        assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
    }

    #[test]
    fn a_complete_http_transfer_with_a_corrupt_entry_keeps_the_old_file() {
        let folder = tempfile::tempdir().unwrap();
        let target = folder.path().join("motion.npz");
        fs::write(&target, b"previous export").unwrap();
        let mut body = test_archive();
        let entry_data = body.windows(b"new export".len()).position(|bytes| bytes == b"new export").unwrap();
        body[entry_data] ^= 1;
        let (url, server) = serve(body.len(), move |mut stream| stream.write_all(&body).unwrap());
        let result = save(&target, url);
        server.join().unwrap();
        assert_eq!(result, Err("云端返回的压缩文件不完整或已损坏，请重试"));
        assert_eq!(fs::read(&target).unwrap(), b"previous export");
        assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
    }

    #[test]
    fn saves_a_deflated_archive_but_rejects_an_empty_archive() {
        let folder = tempfile::tempdir().unwrap();
        let target = folder.path().join("motion.npz");
        let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);
        archive.start_file("motion.npy", options).unwrap();
        archive.write_all(b"new export").unwrap();
        let body = archive.finish().unwrap().into_inner();
        let expected = body.clone();
        let (url, server) = serve(body.len(), move |mut stream| stream.write_all(&body).unwrap());
        assert_eq!(save(&target, url), Ok(expected.len() as u64));
        server.join().unwrap();

        let empty = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let body = empty.finish().unwrap().into_inner();
        let (url, server) = serve(body.len(), move |mut stream| stream.write_all(&body).unwrap());
        let result = save(&target, url);
        server.join().unwrap();
        assert_eq!(result, Err("云端返回的压缩文件不完整或已损坏，请重试"));
        assert_eq!(fs::read(&target).unwrap(), expected);
        assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
    }

    const VALID_BVH: &str = "HIERARCHY\nROOT Pelvis\n{\n  OFFSET 0 0 0\n  CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation\n  End Site\n  {\n    OFFSET 0 1 0\n  }\n}\nMOTION\nFrames: 1\nFrame Time: 0.03333333\n0 100 0 0 0 0\n";

    #[test]
    fn still_saves_a_plain_text_bvh() {
        let folder = tempfile::tempdir().unwrap();
        let target = folder.path().join("motion.bvh");
        let nested = VALID_BVH.replace("ROOT Pelvis\n{", "ROOT Pelvis{")
            .replace("  End Site", "  JOINT Torso{ OFFSET 0 1 0 CHANNELS 3 Xrotation Yrotation Zrotation }\n  End Site")
            .replace("0 100 0 0 0 0", "0 100 0 0 0 0 0 0 0");
        for expected in [VALID_BVH.as_bytes(), nested.as_bytes()] {
            let body = expected.to_vec();
            let (url, server) = serve_response("200 OK", body.len(), "Content-Type: text/plain\r\n", move |mut stream| {
                stream.write_all(&body).unwrap();
            });
            assert_eq!(save_as(&target, url, DownloadFormat::Bvh), Ok(expected.len() as u64));
            server.join().unwrap();
            assert_eq!(fs::read(&target).unwrap(), expected);
        }
    }

    #[test]
    fn bvh_with_damaged_skeleton_does_not_replace_existing_file() {
        let folder = tempfile::tempdir().unwrap();
        let target = folder.path().join("motion.bvh");
        fs::write(&target, b"previous export").unwrap();
        for body in [
            VALID_BVH.replace("}\nMOTION", "MOTION"),
            VALID_BVH.replace("OFFSET 0 0 0", "OFFSET 0 NaN 0"),
            VALID_BVH.replace("Yrotation", "Xrotation"),
            VALID_BVH.replace("Yrotation", "Ytranslation"),
            VALID_BVH.replace("ROOT Pelvis", "ROOT"),
        ] {
            let (url, server) = serve(body.len(), move |mut stream| stream.write_all(body.as_bytes()).unwrap());
            let result = save_as(&target, url, DownloadFormat::Bvh);
            server.join().unwrap();
            assert!(result.is_err(), "Damaged skeleton was saved");
            assert_eq!(fs::read(&target).unwrap(), b"previous export");
            assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
        }
    }

    #[test]
    fn a_complete_http_transfer_with_invalid_bvh_frames_keeps_the_old_file() {
        let folder = tempfile::tempdir().unwrap();
        let target = folder.path().join("motion.bvh");
        fs::write(&target, b"previous export").unwrap();
        for body in [
            VALID_BVH.replace("Frames: 1", "Frames: 2"),
            VALID_BVH.replace("0 100 0 0 0 0", "0 100 0 0 0"),
            VALID_BVH.replace("0 100 0 0 0 0", "0 NaN 0 0 0 0"),
            format!("{VALID_BVH}0 100 0 0 0 0\n"),
        ] {
            let (url, server) = serve(body.len(), move |mut stream| stream.write_all(body.as_bytes()).unwrap());
            let result = save_as(&target, url, DownloadFormat::Bvh);
            server.join().unwrap();
            assert_eq!(result, Err("云端返回的 BVH 文件不完整或已损坏，请重试"));
            assert_eq!(fs::read(&target).unwrap(), b"previous export");
            assert_eq!(fs::read_dir(folder.path()).unwrap().count(), 1);
        }
    }
}

/// The full web workbench (review, production, storage, admin) in its own window.
/// It loads the remote origin, has no IPC access (see capabilities/default.json)
/// and cannot navigate away from the MotionFarm origin. It keeps its own login.
#[tauri::command]
fn open_workbench(app: AppHandle, state: State<'_, Cloud>) -> Result<(), String> {
    let connection = state.current()?;
    let origin = connection.origin.clone();
    let open_error = || "无法打开网页工作台，请重试".to_string();
    if let Some(window) = app.get_webview_window("workbench") {
        return window.set_focus().map_err(|_| open_error());
    }
    let url = origin.parse().map_err(|_| "服务器地址无效，请检查设置".to_string())?;
    WebviewWindowBuilder::new(&app, "workbench", WebviewUrl::External(url))
        .incognito(true)
        .title("kinora 网页工作台")
        .inner_size(1360.0, 900.0)
        .on_navigation(move |url| policy::same_origin(url.as_str(), &origin))
        .build()
        .map(|_| ())
        .map_err(|_| open_error())
}

#[tauri::command]
async fn clear_session(app: AppHandle, state: State<'_, Cloud>) -> Result<Value, String> {
    let connection = match state.current() { Ok(c) => c, Err(_) => return Ok(json!({ "cleared": false })) };
    let (_, revision) = connection.session.begin_request(false);
    let window = app.get_webview_window("main").ok_or_else(|| "找不到主窗口，请重试".to_string())?;
    let dialog = app.clone();
    let confirmed = tauri::async_runtime::spawn_blocking(move || {
        dialog
            .dialog()
            .message("将删除本机保存的工作区、草稿和 Gadget 路径设置。云端任务会继续运行。")
            .title("清除本机登录状态与草稿？")
            .buttons(MessageDialogButtons::OkCancelCustom("清除".into(), "取消".into()))
            .parent(&window)
            .blocking_show()
    })
    .await
    .map_err(|_| "无法显示清除确认窗口，请重试".to_string())?;
    if !confirmed || state.with_active_connection(&connection, || connection.session.clear_if_current(revision)) != Some(true) {
        return Ok(json!({ "cleared": false }));
    }
    if let Some(window) = app.get_webview_window("workbench") {
        let _ = window.close();
    }
    Ok(json!({ "cleared": true }))
}

/// Use the system browser: remote WebViews can fail silently on client machines.
#[tauri::command]
async fn open_account(state: State<'_, Cloud>, mode: String) -> Result<(), String> {
    let url = account::page_url(&state.current()?.origin, &mode)?;
    // Linux launchers may live as long as the browser. Detach after observing
    // immediate launch failures so account buttons do not stay disabled.
    tauri::async_runtime::spawn_blocking(move || open::that_detached(url))
        .await
        .map_err(|_| "无法启动浏览器，请检查系统默认浏览器设置".to_string())?
        .map_err(|_| "无法启动浏览器，请检查系统默认浏览器设置".to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(single_instance::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Updates::default())
        .setup(|app| {
            let client = reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .user_agent(concat!("MotionFarm-Desktop/", env!("CARGO_PKG_VERSION")))
                .build()?;
            let directory = app.path().app_local_data_dir()?;
            let preference_dir = app.path().app_config_dir().ok();
            let origin = server::load_origin(&directory, preference_dir.as_deref());
            let preference_unavailable = AtomicBool::new(origin.is_err());
            let connection = RwLock::new(origin.ok().flatten().map(|origin| server::connection(&directory, origin)));
            app.manage(Cloud { client, connection, directory, preference_dir, preference_unavailable });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            credentials::load_credentials, credentials::save_credentials, credentials::forget_credentials,
            status, configure_server, cloud, cloud_binary, download, open_workbench, open_account, clear_session,
            updates::check_update, updates::download_update, updates::install_update, releases::acknowledge_release,
            #[cfg(target_os = "macos")]
            installations::list_installations,
            #[cfg(target_os = "macos")]
            installations::reveal_installation,
        ])
        .run(tauri::generate_context!())
        .expect("error while running kinora");
}
