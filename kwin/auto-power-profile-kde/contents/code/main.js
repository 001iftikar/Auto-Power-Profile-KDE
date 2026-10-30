/*
 * Auto Power Profile KDE — KWin 6 script package.
 *
 * KWin owns window detection and this script's lifecycle. Python owns all
 * power-state decisions and profile changes. The script sends lifecycle
 * events over the session bus and periodically reconciles its in-memory map
 * so a daemon restart cannot leave stale performance-window state behind.
 */
(function () {
    "use strict";

    var PREFIX = "AutoPowerProfileKDE\t";
    var DAEMON_BUS_NAME = "io.github.iftikar.AutoPowerProfileKDE";
    var DAEMON_OBJECT_PATH = "/io/github/iftikar/AutoPowerProfileKDE";
    var DAEMON_INTERFACE = "io.github.iftikar.AutoPowerProfileKDE.Tracker";

    // Keyed by KWin's stable per-window UUID. Values contain the latest
    // desktopFileName, which can be resolved after windowAdded is emitted.
    var windows = Object.create(null);
    var daemonReachable = false;
    var syncInFlight = false;

    function windowId(win) {
        try {
            return String(win.internalId);
        } catch (error) {
            return "";
        }
    }

    function appId(win) {
        try {
            return String(win.desktopFileName || "").trim();
        } catch (error) {
            return "";
        }
    }

    function caption(win) {
        try {
            return String(win.caption || "").replace(/[\t\r\n]/g, " ");
        } catch (error) {
            return "<unavailable>";
        }
    }

    function isCandidateWindow(win) {
        try {
            return !!win &&
                !win.deleted &&
                !win.specialWindow &&
                !win.popupWindow;
        } catch (error) {
            return false;
        }
    }

    function emitDebug(id, message) {
        print(PREFIX + "DEBUG\t" + id + "\t" + message);
    }

    function buildSnapshot() {
        var snapshot = Object.create(null);
        var ids = Object.keys(windows);
        for (var i = 0; i < ids.length; i++) {
            var entry = windows[ids[i]];
            if (entry && entry.app) {
                snapshot[ids[i]] = entry.app;
            }
        }
        return JSON.stringify(snapshot);
    }

    function reportWindowAdded(id, applicationId) {
        if (!daemonReachable) {
            return;
        }
        callDBus(
            DAEMON_BUS_NAME,
            DAEMON_OBJECT_PATH,
            DAEMON_INTERFACE,
            "WindowAdded",
            id,
            applicationId,
            function (accepted) {
                if (accepted !== true) {
                    emitDebug(id, "Daemon did not acknowledge WindowAdded");
                }
            }
        );
    }

    function reportWindowRemoved(id, applicationId) {
        if (!daemonReachable) {
            return;
        }
        callDBus(
            DAEMON_BUS_NAME,
            DAEMON_OBJECT_PATH,
            DAEMON_INTERFACE,
            "WindowRemoved",
            id,
            applicationId,
            function (accepted) {
                if (accepted !== true) {
                    emitDebug(id, "Daemon did not acknowledge WindowRemoved");
                }
            }
        );
    }

    function removeTracked(id) {
        var entry = windows[id];
        if (!entry || !entry.app) {
            return;
        }

        var previousApp = entry.app;
        entry.app = "";
        reportWindowRemoved(id, previousApp);
        emitDebug(id, "removed " + previousApp);
    }

    function refreshApplicationIdentity(win) {
        var id = windowId(win);
        if (!id || !windows[id]) {
            return;
        }

        var entry = windows[id];
        var currentApp = appId(win);

        if (!currentApp) {
            var diagnostic = "desktopFileName is empty; caption=" + caption(win);
            if (entry.lastDiagnostic !== diagnostic) {
                entry.lastDiagnostic = diagnostic;
                emitDebug(id, diagnostic);
            }
            return;
        }

        if (entry.app === currentApp) {
            return;
        }

        // The identity can resolve after windowAdded or change later.
        removeTracked(id);
        entry.app = currentApp;
        entry.lastDiagnostic = "";
        reportWindowAdded(id, currentApp);
        emitDebug(id, "added " + currentApp);
    }

    function registerWindow(win) {
        if (!isCandidateWindow(win)) {
            return;
        }

        var id = windowId(win);
        if (!id) {
            emitDebug("unknown", "Window has no internalId");
            return;
        }

        if (windows[id]) {
            refreshApplicationIdentity(win);
            return;
        }

        windows[id] = {
            app: "",
            lastDiagnostic: ""
        };

        emitDebug(
            id,
            "windowAdded; desktopFileName=[" + appId(win) + "]; pid=" +
                win.pid + "; caption=" + caption(win)
        );

        try {
            win.desktopFileNameChanged.connect(function () {
                refreshApplicationIdentity(win);
            });
        } catch (error) {
            emitDebug(id, "Could not connect desktopFileNameChanged: " + error);
        }

        try {
            win.windowClassChanged.connect(function () {
                refreshApplicationIdentity(win);
            });
        } catch (error) {
            emitDebug(id, "Could not connect windowClassChanged: " + error);
        }

        refreshApplicationIdentity(win);
    }

    function unregisterWindow(win) {
        var id = windowId(win);
        if (!id || !windows[id]) {
            return;
        }

        removeTracked(id);
        delete windows[id];
    }

    // KWin's callDBus() intentionally does not invoke its callback on a D-Bus
    // error. A watchdog therefore releases syncInFlight, allowing another
    // attempt once the Python daemon has acquired its well-known bus name.
    var syncWatchdog = new QTimer();
    syncWatchdog.setSingleShot(true);
    syncWatchdog.setInterval(2500);
    syncWatchdog.timeout.connect(function () {
        syncInFlight = false;
    });

    var retryTimer = new QTimer();
    retryTimer.setInterval(1000);

    var heartbeatTimer = new QTimer();
    heartbeatTimer.setInterval(10000);

    function syncWindowSnapshot(reason) {
        if (syncInFlight) {
            return;
        }

        syncInFlight = true;
        syncWatchdog.start();
        var payload = buildSnapshot();

        callDBus(
            DAEMON_BUS_NAME,
            DAEMON_OBJECT_PATH,
            DAEMON_INTERFACE,
            "SyncWindows",
            payload,
            function (accepted) {
                syncWatchdog.stop();
                syncInFlight = false;

                if (accepted !== true) {
                    if (daemonReachable) {
                        daemonReachable = false;
                        heartbeatTimer.stop();
                        retryTimer.start();
                    }
                    emitDebug("daemon", "Snapshot sync was not acknowledged (" + reason + ")");
                    return;
                }

                if (!daemonReachable) {
                    daemonReachable = true;
                    retryTimer.stop();
                    heartbeatTimer.start();
                    print(PREFIX + "CONNECTED\tperformance-window-map synchronized");

                    // Re-send the newest map after the first handshake. Window
                    // events that occurred while the daemon was unavailable
                    // were held locally in `windows` and are included here.
                    syncWindowSnapshot("post-connect");
                } else {
                    emitDebug("daemon", "Window snapshot synchronized (" + reason + ")");
                }
            }
        );
    }

    retryTimer.timeout.connect(function () {
        syncWindowSnapshot("connect-retry");
    });
    heartbeatTimer.timeout.connect(function () {
        syncWindowSnapshot("daemon-reconciliation");
    });

    // Subscribe first, then take a one-time snapshot of windows that already
    // exist. All later changes arrive through KWin's lifecycle signals.
    workspace.windowAdded.connect(registerWindow);
    workspace.windowRemoved.connect(unregisterWindow);

    var existingWindows = workspace.stackingOrder;
    for (var i = 0; i < existingWindows.length; i++) {
        registerWindow(existingWindows[i]);
    }

    print(PREFIX + "START\tinitial_windows=" + existingWindows.length);

    // The systemd user service may start after KWin. Retry only the D-Bus
    // handshake; do not scan windows or poll battery/process state.
    retryTimer.start();
    syncWindowSnapshot("startup");
})();
