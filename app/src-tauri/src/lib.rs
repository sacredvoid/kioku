use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent};

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

fn start_engine() -> Option<Child> {
    if engine_running() {
        return None;
    }
    match Command::new(uv_path()).args(["run", "server.py"]).current_dir(engine_dir()).spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("kioku: could not start engine: {e}");
            None
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .setup(|app| {
            app.manage(Engine(Mutex::new(start_engine())));
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
