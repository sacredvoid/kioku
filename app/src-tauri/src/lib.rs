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

// ponytail: engine path is baked in at compile time (fine for a personal app); bundle it as a sidecar to ship elsewhere.
fn engine_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../engine")
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

fn start_engine() -> Option<Child> {
    if engine_running() {
        return None;
    }
    // Launched from Finder or at login, the app gets PATH=/usr/bin:/bin:...; the engine calls ffmpeg, ffprobe
    // and claude by name, which live in Homebrew and ~/.local/bin.
    let home = std::env::var("HOME").unwrap_or_default();
    let path = format!("{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:{}", std::env::var("PATH").unwrap_or_default());
    match Command::new(uv_path()).args(["run", "server.py"]).current_dir(engine_dir()).env("PATH", path).spawn() {
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
            app.manage(Engine(Mutex::new(start_engine())));
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
