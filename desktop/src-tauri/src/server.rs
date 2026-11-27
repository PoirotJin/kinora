//! Non-secret server selection. Credentials and sessions are scoped to this origin.
use std::{fs, io::{self, Write}, path::{Path, PathBuf}, sync::Arc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use url::{Host, Url};

#[derive(Serialize, Deserialize)]
struct Saved { origin: String }

pub struct Connection {
    pub origin: String,
    pub session: crate::session::Session,
}

pub fn loopback(url: &Url) -> bool {
    match url.host() {
        Some(Host::Domain(name)) => name == "localhost",
        Some(Host::Ipv4(ip)) => ip.is_loopback(),
        Some(Host::Ipv6(ip)) => ip.is_loopback(),
        None => false,
    }
}

pub fn normalize(input: &str) -> Result<String, String> {
    let input = input.trim();
    if input.is_empty() || input.len() > 2048 || input.chars().any(char::is_whitespace) || input.contains('\\') {
        return Err("请输入有效的服务器地址".into());
    }
    let explicit = input.contains("://");
    let candidate = if explicit { input.to_owned() } else {
        let probe = Url::parse(&format!("https://{input}")).map_err(|_| "地址格式无效")?;
        format!("{}://{input}", if loopback(&probe) { "http" } else { "https" })
    };
    let mut url = Url::parse(&candidate)
        .map_err(|_| "地址格式无效，请输入 IP、域名或完整 URL")?;
    if !matches!(url.scheme(), "http" | "https") || url.host().is_none() || !url.username().is_empty()
        || url.password().is_some() || url.query().is_some() || url.fragment().is_some() || url.path() != "/" {
        return Err("请输入服务器根地址，不要包含账号、密码、路径或查询参数".into());
    }
    if !explicit && loopback(&url) {
        let authority = input.trim_end_matches('/');
        let has_port = if authority.starts_with('[') { authority.contains("]:") } else { authority.contains(':') };
        if !has_port { url.set_port(Some(8765)).map_err(|_| "端口无效")?; }
    }
    if url.scheme() == "http" && !loopback(&url) {
        return Err("远程服务器请使用 HTTPS；HTTP 仅用于 localhost 或回环 IP".into());
    }
    if url.port() == Some(0) { return Err("端口必须在 1–65535 之间".into()); }
    Ok(url.origin().ascii_serialization())
}

fn session_path(dir: &Path, origin: &str) -> PathBuf {
    let hash = format!("{:x}", Sha256::digest(origin.as_bytes()));
    dir.join(format!("session-{hash}.json"))
}

pub fn connection(dir: &Path, origin: String) -> Arc<Connection> {
    let session = crate::session::Session::load(Some(session_path(dir, &origin)));
    Arc::new(Connection { origin, session })
}

const SERVER_FILE: &str = "server.json";
const RECOVERY_FILE: &str = "server-preference.json";

fn read_preference(path: &Path, legacy_text: bool) -> io::Result<Option<String>> {
    let raw = match fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let origin = if legacy_text { raw } else { serde_json::from_str::<Saved>(&raw)?.origin };
    normalize(&origin).map(Some).map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn write_preference(path: &Path, origin: &str) -> io::Result<()> {
    let parent = path.parent().ok_or(io::ErrorKind::InvalidInput)?;
    fs::create_dir_all(parent)?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer(&mut file, &Saved { origin: origin.to_owned() })?;
    file.flush()?;
    file.as_file().sync_all()?;
    file.persist(path).map_err(|error| error.error)?;
    Ok(())
}

pub fn load_origin(dir: &Path, preference_dir: Option<&Path>) -> io::Result<Option<String>> {
    let primary = dir.join(SERVER_FILE);
    let recovery = preference_dir.map(|dir| dir.join(RECOVERY_FILE));
    let preferred = read_preference(&primary, false);
    let backup = recovery.as_deref().map_or(Ok(None), |path| read_preference(path, false));

    // The original location stays authoritative so a downgrade can still edit
    // the address. The config directory also survives local-data cleanup.
    if let Ok(Some(origin)) = preferred.as_ref() {
        if backup.as_ref().ok().and_then(|value| value.as_deref()) != Some(origin) {
            if let Some(path) = recovery { let _ = write_preference(&path, origin); }
        }
        return Ok(Some(origin.clone()));
    }

    if let Ok(Some(origin)) = backup.as_ref() {
        let _ = write_preference(&primary, origin);
        return Ok(Some(origin.clone()));
    }

    // Older desktop builds persisted a text file in the same per-user directory.
    if let Some(origin) = read_preference(&dir.join("server.txt"), true)? {
        let _ = save(dir, preference_dir, &origin);
        return Ok(Some(origin));
    }
    preferred?;
    backup?;
    Ok(None)
}

pub fn save(dir: &Path, preference_dir: Option<&Path>, origin: &str) -> Result<(), String> {
    write_preference(&dir.join(SERVER_FILE), origin)
        .map_err(|_| "无法保存服务器配置，请重试".to_string())?;
    if let Some(dir) = preference_dir {
        // The committed selection remains usable if the optional mirror is
        // unavailable. It is refreshed again when the app next starts.
        let _ = write_preference(&dir.join(RECOVERY_FILE), origin);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn address_forms_and_transport_boundaries() {
        for (input, expected) in [("localhost:8765", "http://localhost:8765"), ("127.0.0.1:9000", "http://127.0.0.1:9000"),
            ("localhost:80", "http://localhost"), ("localhost:443", "http://localhost:443"),
            ("localhost", "http://localhost:8765"), ("[::1]:8765", "http://[::1]:8765"),
            ("192.0.2.22:8765", "https://192.0.2.22:8765"), (" https://example.com/ ", "https://example.com") ] {
            assert_eq!(normalize(input).unwrap(), expected);
        }
        for input in ["", "http://example.com", "http://localhost.evil.com", "https://user:pass@example.com", "https://example.com/path",
            "https://example.com?key=secret", "https://example.com#fragment", "file:///tmp", "localhost:0", "localhost:99999", "local host:8765", "https://example.com\\@evil.com"] {
            assert!(normalize(input).is_err(), "{input}");
        }
    }
    #[test]
    fn first_launch_persistence_and_server_isolation() {
        let temp = tempfile::tempdir().unwrap();
        let dir = temp.path().join("local-data");
        let prefs = temp.path().join("config");
        assert_eq!(load_origin(&dir, Some(&prefs)).unwrap(), None);
        let a = "http://localhost:8765";
        let b = "https://example.com";
        save(&dir, Some(&prefs), a).unwrap();
        assert_eq!(load_origin(&dir, Some(&prefs)).unwrap().as_deref(), Some(a));
        let first = connection(&dir, a.into());
        first.session.apply(["mf_session=synthetic; Max-Age=3600"].into_iter());
        let second = connection(&dir, b.into());
        assert!(second.session.cookie_header().is_none());
        let cloud = crate::Cloud { client: reqwest::Client::new(), directory: dir.clone(),
            connection: std::sync::RwLock::new(Some(first.clone())), preference_dir: Some(prefs.clone()),
            preference_unavailable: std::sync::atomic::AtomicBool::new(false) };
        assert!(cloud.current_for(a).is_ok());
        *cloud.connection.write().unwrap() = Some(second);
        assert!(cloud.current_for(a).is_err(), "Queued requests for the old server must be rejected");
        assert!(cloud.current_for(b).is_ok());
        first.session.apply(["mf_session=late-response; Max-Age=3600"].into_iter());
        assert!(connection(&dir, b.into()).session.cookie_header().is_none());
        save(&dir, Some(&prefs), b).unwrap();
        fs::remove_file(dir.join(SERVER_FILE)).unwrap();
        assert_eq!(load_origin(&dir, Some(&prefs)).unwrap().as_deref(), Some(b));
        assert_eq!(read_preference(&dir.join(SERVER_FILE), false).unwrap().as_deref(), Some(b));
        fs::write(dir.join(SERVER_FILE), "broken").unwrap();
        assert_eq!(load_origin(&dir, Some(&prefs)).unwrap().as_deref(), Some(b));
        fs::remove_file(prefs.join(RECOVERY_FILE)).unwrap();
        fs::write(dir.join(SERVER_FILE), "broken").unwrap();
        assert!(load_origin(&dir, Some(&prefs)).is_err());
        fs::write(dir.join("server.txt"), a).unwrap();
        assert_eq!(load_origin(&dir, Some(&prefs)).unwrap().as_deref(), Some(a));
        assert_eq!(read_preference(&dir.join(SERVER_FILE), false).unwrap().as_deref(), Some(a));
        assert_eq!(read_preference(&prefs.join(RECOVERY_FILE), false).unwrap().as_deref(), Some(a));
    }

    #[test]
    fn migrates_existing_preferences_without_reverting_an_edit_in_an_older_version() {
        let temp = tempfile::tempdir().unwrap();
        let dir = temp.path().join("local-data");
        let prefs = temp.path().join("config");
        fs::create_dir(&dir).unwrap();
        fs::write(dir.join("server.txt"), "https://192.0.2.22:8765").unwrap();
        let origin = "https://192.0.2.22:8765";
        let edited = "https://198.51.100.23:9876";
        assert_eq!(load_origin(&dir, Some(&prefs)).unwrap().as_deref(), Some(origin));
        assert_eq!(read_preference(&prefs.join(RECOVERY_FILE), false).unwrap().as_deref(), Some(origin));
        write_preference(&dir.join(SERVER_FILE), edited).unwrap();
        assert_eq!(load_origin(&dir, Some(&prefs)).unwrap().as_deref(), Some(edited));
        assert_eq!(read_preference(&prefs.join(RECOVERY_FILE), false).unwrap().as_deref(), Some(edited));
    }
}
