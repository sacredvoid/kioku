use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};

const ENGINE_ADDR: &str = "127.0.0.1:8765";

/// The Python engine we started (None if one was already running, e.g. from a terminal).
struct Engine(Mutex<Option<Child>>);

fn engine_running() -> bool {
    let addr: SocketAddr = ENGINE_ADDR.parse().expect("valid engine address");
    TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok()
}

/// True if the engine on the port answers /health with this app's version. An engine left over from an older
/// build (or one that is frozen) keeps the port and the new app would talk to old code.
fn engine_is_ours() -> bool {
    use std::io::{Read, Write};
    let addr: SocketAddr = ENGINE_ADDR.parse().expect("valid engine address");
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else { return false };
    let _ = s.set_read_timeout(Some(Duration::from_secs(5)));
    let _ = s.set_write_timeout(Some(Duration::from_secs(1)));
    if s.write_all(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").is_err() {
        return false;
    }
    let mut body = String::new();
    let _ = s.read_to_string(&mut body);
    body.contains(&format!("\"version\":\"{}\"", env!("CARGO_PKG_VERSION")))
}

/// Stop whatever is listening on the engine port (SIGTERM, then SIGKILL), and wait for the port to free.
fn stop_stale_engine() {
    let pids = Command::new("/usr/sbin/lsof").args(["-ti", "tcp:8765", "-sTCP:LISTEN"]).output().map(|o| String::from_utf8_lossy(&o.stdout).split_whitespace().map(String::from).collect::<Vec<_>>()).unwrap_or_default();
    if pids.is_empty() {
        return;
    }
    eprintln!("kioku: replacing an engine from another version (pid {})", pids.join(" "));
    for sig in ["-TERM", "-KILL"] {
        let _ = Command::new("/bin/kill").arg(sig).args(&pids).status();
        for _ in 0..30 {
            if !engine_running() {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }
}

/// Where the engine code lives. Release builds run the copy bundled in Kioku.app/Contents/Resources/engine, so an
/// installed version never changes when the repo checkout does. `tauri dev` runs the repo's live code.
fn engine_dir(app: &tauri::App) -> PathBuf {
    if cfg!(debug_assertions) {
        return PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../engine");
    }
    app.path().resource_dir().expect("resource dir").join("engine")
}

fn uv_path() -> PathBuf {
    // GUI apps don't inherit the shell PATH, so look where the uv installer puts it first.
    let home = std::env::var("HOME").unwrap_or_default();
    [format!("{home}/.local/bin/uv"), "/opt/homebrew/bin/uv".into()]
        .into_iter()
        .map(PathBuf::from)
        .find(|p| p.exists())
        .unwrap_or_else(|| PathBuf::from("uv"))
}

/// Ask macOS for Photos access from the app itself. The engine (a child process) downloads iCloud originals
/// through PhotoKit, but a background process can't raise the permission prompt, and the Photos pane in
/// System Settings has no "+" to add an app by hand. Only asks while the status is "not determined".
#[cfg(target_os = "macos")]
fn request_photos_access() {
    use block2::RcBlock;
    use objc2::{class, msg_send};
    #[link(name = "Photos", kind = "framework")]
    unsafe extern "C" {}
    const READ_WRITE: isize = 2; // PHAccessLevelReadWrite
    let status: isize = unsafe { msg_send![class!(PHPhotoLibrary), authorizationStatusForAccessLevel: READ_WRITE] };
    if status != 0 {
        return; // already granted, limited, denied or restricted: nothing to ask
    }
    let handler = RcBlock::new(|_status: isize| {});
    unsafe {
        let _: () = msg_send![class!(PHPhotoLibrary), requestAuthorizationForAccessLevel: READ_WRITE, handler: &*handler];
    }
}

fn start_engine(engine: PathBuf) -> Option<Child> {
    if engine_running() {
        if engine_is_ours() {
            return None;
        }
        stop_stale_engine();
    }
    // Launched from Finder or at login, the app gets PATH=/usr/bin:/bin:...; the engine calls ffmpeg, ffprobe
    // and claude by name, which live in Homebrew and ~/.local/bin.
    let home = std::env::var("HOME").unwrap_or_default();
    let path = format!("{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:{}", std::env::var("PATH").unwrap_or_default());
    let mut cmd = Command::new(uv_path());
    cmd.args(["run", "server.py"]).current_dir(&engine).env("PATH", path);
    if !cfg!(debug_assertions) {
        // The bundle is read-only and signed: the venv and bytecode go to ~/Kioku, and uv.lock is used as shipped.
        let data = PathBuf::from(&home).join("Kioku");
        cmd.env("UV_PROJECT_ENVIRONMENT", data.join("venv")).env("UV_FROZEN", "1").env("PYTHONPYCACHEPREFIX", data.join("pycache"));
    }
    match cmd.spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("kioku: could not start engine: {e}");
            None
        }
    }
}

/// Open at login, turned on once on first launch. A marker file keeps it from coming back after Sam
/// switches it off in System Settings > General > Login Items.
fn enable_login_item_once(app: &tauri::App) {
    let Ok(dir) = app.path().app_data_dir() else { return };
    let marker = dir.join("login-item-set");
    if marker.exists() {
        return;
    }
    match app.autolaunch().enable() {
        Ok(()) => {
            let _ = std::fs::create_dir_all(&dir).and_then(|_| std::fs::write(&marker, b""));
        }
        Err(e) => eprintln!("kioku: could not add login item: {e}"),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
        .setup(|app| {
            #[cfg(target_os = "macos")]
            request_photos_access();
            app.manage(Engine(Mutex::new(start_engine(engine_dir(app)))));
            enable_login_item_once(app);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Kioku");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            // Only stop the engine if this app started it.
            if let Some(mut child) = handle.state::<Engine>().0.lock().ok().and_then(|mut g| g.take()) {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    });
}
