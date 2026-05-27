(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect, useRef } = SDK.hooks;

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const existing = document.querySelector(`script[src="${src}"]`);
      if (existing) {
        resolve();
        return;
      }
      const script = document.createElement("script");
      script.src = src;
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  function loadStylesheet(href) {
    const existing = document.querySelector(`link[href="${href}"]`);
    if (existing) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    document.head.appendChild(link);
  }

  function TerminalPage() {
    const hostRef = useRef(null);
    const termRef = useRef(null);
    const fitRef = useRef(null);
    const wsRef = useRef(null);
    const [banner, setBanner] = useState("Loading Terminal...");
    const [xtermLoaded, setXtermLoaded] = useState(false);

    const basePath = (window.__HERMES_BASE_PATH__ || "").replace(/\/+$/, "");
    const getAssetUrl = (p) => basePath + (p.startsWith("/") ? p : "/" + p);

    useEffect(() => {
      loadStylesheet(getAssetUrl("/dashboard-plugins/terminal-plugin/dist/libs/xterm.css"));
      
      Promise.all([
        loadScript(getAssetUrl("/dashboard-plugins/terminal-plugin/dist/libs/xterm.js")),
      ])
        .then(() => {
          return Promise.all([
            loadScript(getAssetUrl("/dashboard-plugins/terminal-plugin/dist/libs/addon-fit.js")),
            loadScript(getAssetUrl("/dashboard-plugins/terminal-plugin/dist/libs/addon-web-links.js")),
            loadScript(getAssetUrl("/dashboard-plugins/terminal-plugin/dist/libs/addon-unicode11.js")),
            loadScript(getAssetUrl("/dashboard-plugins/terminal-plugin/dist/libs/addon-webgl.js")),
          ]);
        })
        .then(() => {
          setXtermLoaded(true);
          setBanner(null);
        })
        .catch((err) => {
          console.error("Failed to load xterm.js:", err);
          setBanner("Failed to load terminal interface: " + (err && err.message || err || "network or path error"));
        });
    }, []);

    useEffect(() => {
      if (!xtermLoaded) return;
      const host = hostRef.current;
      if (!host) return;

      const token = window.__HERMES_SESSION_TOKEN__;
      if (!token) {
        setBanner("Session token unavailable. Reload the page.");
        return;
      }

      const { Terminal } = window;
      const { FitAddon } = window.FitAddon || {};
      const { WebLinksAddon } = window.WebLinksAddon || {};
      const { Unicode11Addon } = window.Unicode11Addon || {};
      const { WebglAddon } = window.WebglAddon || {};

      const term = new Terminal({
        allowProposedApi: true,
        cursorBlink: true,
        fontFamily: "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace",
        fontSize: 13,
        lineHeight: 1.2,
        letterSpacing: 0,
        fontWeight: "400",
        fontWeightBold: "700",
        macOptionIsMeta: true,
        macOptionClickForcesSelection: true,
        rightClickSelectsWord: true,
        scrollback: 5000,
        theme: {
          background: "#0d1117",
          foreground: "#c9d1d9",
          cursor: "#58a6ff",
          cursorAccent: "#0d1117",
          selectionBackground: "#58a6ff44",
        },
      });
      termRef.current = term;

      const isMac = typeof navigator !== "undefined" && /Mac/i.test(navigator.platform);
      term.attachCustomKeyEventHandler((ev) => {
        if (ev.type !== "keydown") return true;
        const copyModifier = isMac ? ev.metaKey : ev.ctrlKey && ev.shiftKey;
        const pasteModifier = isMac ? ev.metaKey : ev.ctrlKey && ev.shiftKey;

        if (copyModifier && ev.key.toLowerCase() === "c") {
          const sel = term.getSelection();
          if (sel) {
            navigator.clipboard.writeText(sel).catch((err) => {
              console.warn("[terminal clipboard] copy failed:", err.message);
            });
            term.clearSelection();
            ev.preventDefault();
            return false;
          }
        }
        if (pasteModifier && ev.key.toLowerCase() === "v") {
          navigator.clipboard.readText()
            .then((text) => {
              if (text) term.paste(text);
            })
            .catch((err) => {
              console.warn("[terminal clipboard] paste failed:", err.message);
            });
          ev.preventDefault();
          return false;
        }
        return true;
      });

      let fit = null;
      if (FitAddon) {
        fit = new FitAddon();
        fitRef.current = fit;
        term.loadAddon(fit);
      }

      if (Unicode11Addon) {
        const unicode11 = new Unicode11Addon();
        term.loadAddon(unicode11);
        term.unicode.activeVersion = "11";
      }

      if (WebLinksAddon) {
        term.loadAddon(new WebLinksAddon());
      }

      term.open(host);

      if (WebglAddon) {
        try {
          const webgl = new WebglAddon();
          webgl.onContextLoss(() => webgl.dispose());
          term.loadAddon(webgl);
        } catch (e) {
          console.warn("WebGL renderer unavailable; falling back to canvas", e);
        }
      }

      let metricsDebounce = null;
      const syncTerminalMetrics = () => {
        if (!host.isConnected || host.clientWidth <= 0 || host.clientHeight <= 0) return;
        try {
          if (fit) fit.fit();
        } catch (err) {
          return;
        }
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          wsRef.current.send(`\x1b[RESIZE:${term.cols};${term.rows}]`);
        }
      };

      const scheduleSyncTerminalMetrics = () => {
        if (metricsDebounce) clearTimeout(metricsDebounce);
        metricsDebounce = setTimeout(syncTerminalMetrics, 60);
      };

      const ro = new ResizeObserver(scheduleSyncTerminalMetrics);
      ro.observe(host);
      window.addEventListener("resize", scheduleSyncTerminalMetrics);

      requestAnimationFrame(() => {
        syncTerminalMetrics();
      });

      let unmounting = false;
      let onDataDisposable = null;
      let onResizeDisposable = null;

      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const qs = new URLSearchParams({ token });
      const url = `${proto}//${window.location.host}${basePath}/api/plugins/terminal-plugin/terminal/pty?${qs.toString()}`;
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        setBanner(null);
        ws.send(`\x1b[RESIZE:${term.cols};${term.rows}]`);
      };

      ws.onmessage = (ev) => {
        if (typeof ev.data === "string") {
          term.write(ev.data);
        } else {
          term.write(new Uint8Array(ev.data));
        }
      };

      ws.onclose = (ev) => {
        wsRef.current = null;
        if (unmounting) return;
        if (ev.code === 4401) {
          setBanner("Auth failed. Reload the page to refresh the session token.");
          return;
        }
        if (ev.code === 4403) {
          setBanner("Terminal is only reachable from localhost.");
          return;
        }
        term.write("\r\n\x1b[90m[session ended]\x1b[0m\r\n");
      };

      onDataDisposable = term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(data);
        }
      });

      onResizeDisposable = term.onResize(({ cols, rows }) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(`\x1b[RESIZE:${cols};${rows}]`);
        }
      });

      term.focus();

      return () => {
        unmounting = true;
        onDataDisposable?.dispose();
        onResizeDisposable?.dispose();
        if (metricsDebounce) clearTimeout(metricsDebounce);
        window.removeEventListener("resize", scheduleSyncTerminalMetrics);
        ro.disconnect();
        wsRef.current?.close();
        wsRef.current = null;
        term.dispose();
        termRef.current = null;
        fitRef.current = null;
      };
    }, [xtermLoaded]);

    return React.createElement(
      "div",
      { className: "flex min-h-0 flex-1 flex-col gap-2 p-4 h-full" },
      banner && React.createElement(
        "div",
        { className: "border border-warning/50 bg-warning/10 text-warning px-3 py-2 text-xs tracking-wide" },
        banner
      ),
      React.createElement(
        "div",
        {
          className: "relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-lg p-3 bg-black",
          style: { boxShadow: "0 8px 32px rgba(0, 0, 0, 0.4)" }
        },
        React.createElement("div", {
          ref: hostRef,
          className: "min-h-0 min-w-0 flex-1"
        })
      )
    );
  }

  window.__HERMES_PLUGINS__.register("terminal-plugin", TerminalPage);
})();
