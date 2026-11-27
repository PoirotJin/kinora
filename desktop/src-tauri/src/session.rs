//! The MotionFarm login cookie (`mf_session`), kept in the main process only and
//! persisted to an owner-only file so a login survives restarts. The renderer
//! never sees it. Other cookies are ignored: the server sets no others.
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

const NAME: &str = "mf_session";
static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
struct Stored {
    token: String,
    expires: u64,
}

pub struct Session {
    path: Option<PathBuf>,
    state: Mutex<State>,
}

struct State {
    value: Option<Stored>,
    revision: u64,
}

/// Outcome of a Set-Cookie header for our cookie.
#[derive(Debug, PartialEq, Eq)]
pub enum Change {
    Set(String, u64),
    Clear,
    Ignore,
}

fn token_ok(value: &str) -> bool {
    !value.is_empty() && value.len() <= 512 && value.bytes().all(|b| b.is_ascii_graphic() && !matches!(b, b';' | b',' | b'"' | b'\\'))
}

pub fn parse_set_cookie(header: &str, now: u64) -> Change {
    let mut parts = header.split(';').map(str::trim);
    let Some((name, value)) = parts.next().and_then(|p| p.split_once('=')) else { return Change::Ignore };
    if name.trim() != NAME {
        return Change::Ignore;
    }
    let value = value.trim();
    let mut max_age: Option<i64> = None;
    for attribute in parts {
        if let Some((key, v)) = attribute.split_once('=') {
            if key.trim().eq_ignore_ascii_case("max-age") {
                max_age = v.trim().parse().ok();
            }
        }
    }
    if value.is_empty() || matches!(max_age, Some(age) if age <= 0) {
        return Change::Clear;
    }
    if !token_ok(value) {
        return Change::Ignore;
    }
    // The server always sends Max-Age; without one, keep it for this run only (1 day cap).
    let ttl = max_age.map(|a| a as u64).unwrap_or(86_400).min(400 * 86_400);
    Change::Set(value.to_string(), now + ttl)
}

impl Session {
    pub fn load(path: Option<PathBuf>) -> Self {
        let value = path
            .as_ref()
            .and_then(|p| fs::read(p).ok())
            .and_then(|raw| serde_json::from_slice::<Stored>(&raw).ok())
            .filter(|s| s.expires > now() && token_ok(&s.token));
        Session { path, state: Mutex::new(State { value, revision: 0 }) }
    }

    pub fn cookie_header(&self) -> Option<String> {
        let state = self.state.lock().ok()?;
        Self::header_for(state.value.as_ref())
    }

    pub fn with_matching_cookie<T>(&self, expected: Option<&str>, action: impl FnOnce() -> T) -> Option<T> {
        let state = self.state.lock().ok()?;
        (Self::header_for(state.value.as_ref()).as_deref() == expected).then(action)
    }

    fn header_for(stored: Option<&Stored>) -> Option<String> {
        stored.filter(|s| s.expires > now()).map(|s| format!("{NAME}={}", s.token))
    }

    pub fn begin_request(&self, logout: bool) -> (Option<String>, u64) {
        let mut state = self.state.lock().unwrap();
        let cookie = Self::header_for(state.value.as_ref());
        if logout {
            self.store(&mut state, None);
        }
        (cookie, state.revision)
    }

    pub fn apply_if_current<'a>(&self, revision: u64, headers: impl Iterator<Item = &'a str>) -> bool {
        let mut state = self.state.lock().unwrap();
        if state.revision != revision {
            return false;
        }
        self.apply_headers(&mut state, headers);
        true
    }

    fn apply_headers<'a>(&self, state: &mut State, headers: impl Iterator<Item = &'a str>) {
        for header in headers {
            match parse_set_cookie(header, now()) {
                Change::Set(token, expires) => self.store(state, Some(Stored { token, expires })),
                Change::Clear => self.store(state, None),
                Change::Ignore => {}
            }
        }
    }

    pub fn clear_if_current(&self, revision: u64) -> bool {
        let mut state = self.state.lock().unwrap();
        if state.revision != revision {
            return false;
        }
        self.store(&mut state, None);
        true
    }

    #[cfg(test)]
    pub fn apply<'a>(&self, headers: impl Iterator<Item = &'a str>) {
        self.apply_headers(&mut self.state.lock().unwrap(), headers);
    }

    #[cfg(test)]
    pub fn clear(&self) {
        self.store(&mut self.state.lock().unwrap(), None);
    }

    fn store(&self, state: &mut State, value: Option<Stored>) {
        // Keep memory and disk writes in the same order, including logout.
        state.value = value.clone();
        state.revision += 1;
        let Some(path) = &self.path else { return };
        match value {
            None => {
                let _ = fs::remove_file(path);
            }
            Some(stored) => {
                if let Some(parent) = path.parent() {
                    let _ = fs::create_dir_all(parent);
                }
                let stamp = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos();
                let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
                let temp = path.with_extension(format!("{}.{stamp}.{sequence}.tmp", std::process::id()));
                let mut options = fs::OpenOptions::new();
                options.write(true).create_new(true);
                #[cfg(unix)]
                {
                    use std::os::unix::fs::OpenOptionsExt;
                    options.mode(0o600);
                }
                if let Ok(mut file) = options.open(&temp) {
                    let written = file.write_all(&serde_json::to_vec(&stored).unwrap_or_default()).is_ok() && file.sync_all().is_ok();
                    drop(file);
                    if written {
                        let _ = fs::rename(&temp, path);
                    }
                    let _ = fs::remove_file(&temp);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_server_cookie_forms() {
        let set = "mf_session=abc123; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800; Secure";
        assert_eq!(parse_set_cookie(set, 100), Change::Set("abc123".into(), 100 + 604_800));
        assert_eq!(parse_set_cookie("mf_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0", 100), Change::Clear);
        assert_eq!(parse_set_cookie("other=1; Max-Age=10", 100), Change::Ignore);
        assert_eq!(parse_set_cookie("mf_session=a;b", 100), Change::Set("a".into(), 100 + 86_400));
        assert_eq!(parse_set_cookie("mf_session=bad value; Max-Age=10", 100), Change::Ignore);
        assert_eq!(parse_set_cookie("garbage", 100), Change::Ignore);
    }

    #[test]
    fn persists_and_clears() {
        let dir = std::env::temp_dir().join(format!("mf-session-test-{}", std::process::id()));
        let path = dir.join("session.json");
        let session = Session::load(Some(path.clone()));
        assert_eq!(session.cookie_header(), None);
        session.apply(["mf_session=tok; Max-Age=3600"].into_iter());
        assert_eq!(Session::load(Some(path.clone())).cookie_header().as_deref(), Some("mf_session=tok"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        }
        session.apply(["mf_session=; Max-Age=0"].into_iter());
        assert!(!path.exists());
        assert_eq!(Session::load(Some(path)).cookie_header(), None);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn logout_invalidates_pending_login_and_old_clear_prompts() {
        let session = Session::load(None);
        let (cookie, pending_login) = session.begin_request(false);
        assert_eq!(cookie, None);
        let (cookie, signed_out) = session.begin_request(true);
        assert_eq!(cookie, None);
        session.apply_if_current(pending_login, ["mf_session=late-login; Max-Age=60"].into_iter());
        assert_eq!(session.cookie_header(), None);
        session.apply_if_current(signed_out, ["mf_session=new-login; Max-Age=60"].into_iter());
        assert!(!session.clear_if_current(signed_out));
        assert_eq!(session.cookie_header().as_deref(), Some("mf_session=new-login"));
    }

    #[test]
    fn concurrent_updates_keep_disk_and_memory_consistent() {
        use std::sync::{Arc, Barrier};
        let dir = std::env::temp_dir().join(format!("mf-session-concurrent-{}", std::process::id()));
        let path = dir.join("session.json");
        let session = Arc::new(Session::load(Some(path.clone())));
        let barrier = Arc::new(Barrier::new(8));
        let threads: Vec<_> = (0..8).map(|worker| {
            let session = Arc::clone(&session);
            let barrier = Arc::clone(&barrier);
            let path = path.clone();
            std::thread::spawn(move || {
                for round in 0..20 {
                    barrier.wait();
                    if (worker + round) % 3 == 0 {
                        session.clear();
                    } else {
                        session.apply([format!("mf_session=worker{worker}-round{round}; Max-Age=3600")].iter().map(String::as_str));
                    }
                    barrier.wait();
                    assert_eq!(Session::load(Some(path.clone())).cookie_header(), session.cookie_header());
                    barrier.wait();
                }
            })
        }).collect();
        for thread in threads { thread.join().unwrap(); }
        session.clear();
        assert!(!path.exists());
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 0);
        let _ = fs::remove_dir_all(dir);
    }
}
