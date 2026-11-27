//! Fixed HTTPS feed; signed artifacts are verified by Tauri before installation.
use std::{sync::Mutex, time::Duration};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_updater::{Update, UpdaterExt};

pub struct Pending {
    pub phase: &'static str,
    pub message: String,
    update: Option<Update>,
    bytes: Option<Vec<u8>>,
}

impl Default for Pending {
    fn default() -> Self {
        Self { phase: "idle", message: "".into(), update: None, bytes: None }
    }
}

#[derive(Default)]
pub struct Updates(pub Mutex<Pending>);

fn publish(app: &AppHandle, phase: &'static str, message: String) {
    {
        let state = app.state::<Updates>();
        let mut pending = state.0.lock().unwrap();
        pending.phase = phase;
        pending.message = message;
    }
    let _ = app.emit("status", super::snapshot(app));
}

fn is_release_download(url: &url::Url) -> bool {
    url.scheme() == "https"
        && url.host_str() == Some("github.com")
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && url.path().starts_with("/PoirotJin/kinora/releases/download/")
}

fn release_updates_enabled(app: &AppHandle) -> bool {
    if app.config().identifier == "com.motionfarm.desktop" { return true; }
    publish(app, "idle", "测试版使用独立安装包更新".into());
    false
}

#[tauri::command]
pub async fn check_update(app: AppHandle) {
    if !release_updates_enabled(&app) { return; }
    let state = app.state::<Updates>();
    {
        let mut pending = state.0.lock().unwrap();
        if matches!(pending.phase, "checking" | "downloading" | "downloaded" | "installing") { return; }
        pending.phase = "checking";
        pending.update = None;
        pending.bytes = None;
    }
    publish(&app, "checking", "正在检查更新…".into());
    let updater = match app.updater_builder().timeout(Duration::from_secs(30)).build() {
        Ok(updater) => updater,
        Err(_) => { publish(&app, "error", "更新配置无效，请联系管理员".into()); return; }
    };
    match updater.check().await {
        Ok(Some(mut update)) => {
            if !is_release_download(&update.download_url) {
                publish(&app, "error", "更新包地址无效".into()); return;
            }
            update.timeout = Some(Duration::from_secs(300));
            let message = format!("可更新至 v{}", update.version);
            state.0.lock().unwrap().update = Some(update);
            publish(&app, "available", message);
        }
        Ok(None) => publish(&app, "idle", "已是最新版本".into()),
        Err(_) => publish(&app, "error", "暂时无法检查更新，请稍后重试（更新包可能尚未发布）".into()),
    }
}

#[tauri::command]
pub async fn download_update(app: AppHandle) {
    if !release_updates_enabled(&app) { return; }
    let state = app.state::<Updates>();
    let update = {
        let mut pending = state.0.lock().unwrap();
        if pending.phase != "available" { return; }
        pending.phase = "downloading";
        pending.update.take()
    };
    let Some(update) = update else { publish(&app, "error", "请重新检查更新".into()); return; };
    publish(&app, "downloading", "正在下载更新…".into());
    let mut received = 0u64;
    let mut last_percent = None;
    let result = update.download(|chunk, total| {
        received += chunk as u64;
        let percent = total.filter(|n| *n > 0).map(|n| (received.saturating_mul(100) / n).min(100));
        if percent != last_percent {
            last_percent = percent;
            publish(&app, "downloading", format!("正在下载更新 {}%", percent.unwrap_or(0)));
        }
    }, || {}).await;
    match result {
        Ok(bytes) => {
            let mut pending = state.0.lock().unwrap();
            pending.bytes = Some(bytes);
            pending.update = Some(update);
            drop(pending);
            publish(&app, "downloaded", "更新已下载并验证，可选择重启安装".into());
        }
        Err(_) => publish(&app, "error", "下载或签名校验失败，请重新检查更新".into()),
    }
}

#[tauri::command]
pub async fn install_update(app: AppHandle) {
    if !release_updates_enabled(&app) { return; }
    let state = app.state::<Updates>();
    let package = {
        let mut pending = state.0.lock().unwrap();
        if pending.phase != "downloaded" { return; }
        pending.phase = "installing";
        pending.update.take().zip(pending.bytes.take())
    };
    let Some((update, bytes)) = package else { publish(&app, "error", "请重新下载更新".into()); return; };
    publish(&app, "installing", "正在安装，请勿关闭应用…".into());
    let result = tauri::async_runtime::spawn_blocking(move || update.install(bytes)).await;
    match result {
        Ok(Ok(())) => app.restart(),
        _ => publish(&app, "error", "安装失败，请重新下载或使用新版安装包".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::is_release_download;

    #[test]
    fn downloads_must_come_from_the_kinora_release() {
        for (url, expected) in [
            ("https://github.com/PoirotJin/kinora/releases/download/kinora-v0.9.0/kinora.exe", true),
            ("http://github.com/PoirotJin/kinora/releases/download/kinora-v0.9.0/kinora.exe", false),
            ("https://github.com/PoirotJin/other/releases/download/kinora-v0.9.0/kinora.exe", false),
            ("https://github.com.evil.test/PoirotJin/kinora/releases/download/file", false),
            ("https://user@github.com/PoirotJin/kinora/releases/download/file", false),
        ] {
            assert_eq!(is_release_download(&url::Url::parse(url).unwrap()), expected);
        }
    }
}
