// A tiny shim (same idea as afterglow's): LayerShellQt has a C++ API only -- no Python bindings --
// so this exposes the one call the overlay needs as a plain C function that Python loads with
// ctypes (portcullis/overlay/layershell.py).  Build against the SAME Qt as PySide6 (flake.nix).
#include <LayerShellQt/Window>
#include <QMargins>
#include <QWindow>

extern "C" {

// Turns an existing, not-yet-shown QWindow into an overlay-layer surface:
//   layer overlay (above fullscreen windows), no keyboard focus, exclusive zone 0,
//   anchors = LayerShellQt::Window::Anchor bits (top=1 bottom=2 left=4 right=8), margins in px.
// Returns 0 on success, -1 if the window isn't usable as a layer surface.
int portcullis_layershell_configure(void *qwindow, int anchors, int top, int right, int bottom, int left)
{
    auto *window = static_cast<QWindow *>(qwindow);
    if (!window) {
        return -1;
    }
    auto *ls = LayerShellQt::Window::get(window);
    if (!ls) {
        return -1;
    }
    ls->setScope(QStringLiteral("portcullis-overlay"));
    ls->setLayer(LayerShellQt::Window::LayerOverlay);
    ls->setKeyboardInteractivity(LayerShellQt::Window::KeyboardInteractivityNone);
    ls->setExclusiveZone(0);
    ls->setAnchors(LayerShellQt::Window::Anchors(anchors));
    ls->setMargins(QMargins(left, top, right, bottom));
    return 0;
}

} // extern "C"
