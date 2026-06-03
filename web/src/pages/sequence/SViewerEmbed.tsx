/**
 * Lazy, opt-in NCBI Sequence Viewer (SViewer) in-page embed.
 *
 * Responsibility: Render the NCBI Graphical Sequence Viewer inside the Sequence
 * Detail page on explicit user action, using NCBI's official CORS-enabled JS
 * widget API (``sviewer.js`` + ``SeqView.App``). The NCBI script is injected
 * only when the researcher clicks "Load interactive viewer", never on page
 * render, so the third-party script and the browser-to-NCBI traffic surface
 * only activate on demand.
 * Edit boundaries: UI-only. No backend calls; the widget talks to NCBI directly
 * from the browser (public sequence data only). The CSP origins that let the
 * widget load live in ``web/nginx.conf`` and ``api/app/security_headers.py`` —
 * keep them in sync with ``SVIEWER_SCRIPT_SRC`` here.
 * Key entry points: ``SViewerEmbed`` (default + named export).
 * Risky contracts: the container ``<div>`` must already exist in the DOM before
 * ``new SeqView.App(divId)`` runs (NCBI rejects init on a missing/hidden div),
 * so it is rendered as soon as loading starts. ``loadSviewerScript`` is a
 * module-level singleton so repeated mounts never inject ``sviewer.js`` twice.
 * Parents must key this component by accession so a route-param change resets
 * it to the idle state instead of reusing a stale viewer instance.
 * Validation: ``cd web && npm run build`` (type-check). End-to-end embedding
 * requires a frontend deploy because the enabling CSP lives in the nginx
 * sidecar, not in the dev server.
 */
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, ExternalLink, Loader2, Maximize2 } from "lucide-react";

// NCBI dynamically loads ExtJS + CSS from www.ncbi.nlm.nih.gov once this single
// loader script runs; there is nothing else to declare. CORS is supported, so
// no redirect shim is needed.
const SVIEWER_SCRIPT_SRC =
  "https://www.ncbi.nlm.nih.gov/projects/sviewer/js/sviewer.js";

// Namespaced per NCBI guidance ("avoid generic phrases ... prepend a
// namespace"). This name appears in a cookie NCBI sets in the user's browser to
// persist their track configuration.
const SVIEWER_APP_NAME = "ElbDashboardSV";

// Upper bound on the whole load → ready handshake. NCBI normally fires `error`
// on a failed script fetch, but a hung TCP connection or a `SeqViewOnReady`
// callback that never fires would otherwise leave the spinner turning forever
// (no implicit browser timeout on a stalled <script>). Past this, drop to the
// error/degraded state so the "Open in new tab" fallback is offered.
const LOAD_TIMEOUT_MS = 20_000;

interface SeqViewAppInstance {
  load: (params: string) => void;
  destroy?: () => void;
}

interface SeqViewGlobal {
  App: new (divId: string) => SeqViewAppInstance;
}

declare global {
  interface Window {
    SeqView?: SeqViewGlobal;
    // ``SeqViewOnReady`` defers the callback until the SeqView class is fully
    // initialised; NCBI requires it for dynamic (programmatic) instantiation.
    SeqViewOnReady?: (callback: () => void) => void;
  }
}

let scriptPromise: Promise<void> | null = null;

// Inject ``sviewer.js`` exactly once per page, even across multiple mounts.
function loadSviewerScript(): Promise<void> {
  if (typeof window !== "undefined" && window.SeqView) return Promise.resolve();
  if (scriptPromise) return scriptPromise;
  scriptPromise = new Promise<void>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(
      'script[data-sviewer="true"]',
    );
    if (existing) {
      existing.addEventListener("load", () => resolve());
      existing.addEventListener("error", () => {
        // Remove the dead tag so a retry re-injects a fresh script instead of
        // re-attaching to an element whose load/error event already fired and
        // never fires again (which would hang the promise forever).
        scriptPromise = null;
        existing.remove();
        reject(new Error("sviewer script failed to load"));
      });
      return;
    }
    const script = document.createElement("script");
    script.src = SVIEWER_SCRIPT_SRC;
    script.async = true;
    script.dataset.sviewer = "true";
    script.addEventListener("load", () => resolve());
    script.addEventListener("error", () => {
      // Drop the cached promise AND remove the failed tag so a later click can
      // retry from a clean slate (a leftover tag would defeat the retry).
      scriptPromise = null;
      script.remove();
      reject(new Error("sviewer script failed to load"));
    });
    document.head.appendChild(script);
  });
  return scriptPromise;
}

function buildLoadParams(
  accession: string,
  highlight?: { start: number; stop: number } | null,
): string {
  const params = new URLSearchParams();
  // ``embedded=true`` is a required parameter for the embedded widget.
  params.set("embedded", "true");
  params.set("appname", SVIEWER_APP_NAME);
  params.set("id", accession);
  params.set("tracks", "[key:sequence_track,name:Sequence][key:gene_model_track]");
  if (highlight) {
    // ``v`` sets the visible range; ``mk`` drops a named marker on the hit.
    params.set("v", `${highlight.start}:${highlight.stop}`);
    params.set("mk", `${highlight.start}:${highlight.stop}|hit`);
  }
  return params.toString();
}

type EmbedState = "idle" | "loading" | "ready" | "error";

export function SViewerEmbed({
  accession,
  highlight,
  fallbackHref,
}: {
  accession: string;
  highlight?: { start: number; stop: number } | null;
  fallbackHref: string;
}) {
  const [state, setState] = useState<EmbedState>("idle");
  const appRef = useRef<SeqViewAppInstance | null>(null);
  const divIdRef = useRef(`sviewer-${Math.random().toString(36).slice(2, 10)}`);
  // Guards an async init that resolves after the component unmounts or the
  // accession changes (the parent keys us by accession, so that is a remount).
  const mountedRef = useRef(true);
  const timeoutRef = useRef<number | null>(null);

  const clearLoadTimeout = () => {
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  };

  // Tear down the SViewer instance on unmount so it does not leak ExtJS
  // components or keep polling NCBI in the background. The effect body also
  // re-arms `mountedRef`, so React 18 StrictMode's mount→unmount→mount double
  // invoke in development leaves the ref `true` (a stale `false` would make
  // every load silently bail).
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      clearLoadTimeout();
      try {
        appRef.current?.destroy?.();
      } catch {
        // SViewer teardown is best-effort.
      }
      appRef.current = null;
    };
  }, []);

  const handleLoad = () => {
    setState("loading");
    clearLoadTimeout();
    timeoutRef.current = window.setTimeout(() => {
      timeoutRef.current = null;
      if (!mountedRef.current || appRef.current) return;
      console.warn("[SViewerEmbed] NCBI Sequence Viewer load timed out");
      setState("error");
    }, LOAD_TIMEOUT_MS);

    loadSviewerScript()
      .then(() => {
        const init = () => {
          // The component may have unmounted (or remounted under a new
          // accession) while sviewer.js / SeqViewOnReady was pending. Bail so
          // we never instantiate a viewer into a detached div that the unmount
          // cleanup has already passed and can no longer destroy.
          if (!mountedRef.current) return;
          try {
            if (!window.SeqView) throw new Error("SeqView global missing");
            const app = new window.SeqView.App(divIdRef.current);
            appRef.current = app;
            app.load(buildLoadParams(accession, highlight));
            clearLoadTimeout();
            setState("ready");
          } catch (err) {
            console.warn("[SViewerEmbed] failed to initialise viewer", err);
            clearLoadTimeout();
            setState("error");
          }
        };
        if (window.SeqViewOnReady) window.SeqViewOnReady(init);
        else init();
      })
      .catch((err) => {
        if (!mountedRef.current) return;
        console.warn("[SViewerEmbed] failed to load sviewer.js", err);
        clearLoadTimeout();
        setState("error");
      });
  };

  return (
    <div style={{ display: "grid", gap: 10 }}>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        {state === "idle" && (
          <button
            type="button"
            className="glass-button glass-button--primary"
            onClick={handleLoad}
            style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <Maximize2 size={13} strokeWidth={1.5} />
            Load interactive viewer
          </button>
        )}
        {state === "loading" && (
          <span
            className="muted"
            style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12 }}
          >
            <Loader2 size={13} className="spin" aria-hidden="true" />
            Loading the NCBI Sequence Viewer…
          </span>
        )}
        {state === "error" && (
          <>
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
                color: "var(--warning)",
              }}
            >
              <AlertTriangle size={13} strokeWidth={1.5} />
              The interactive viewer could not load.
            </span>
            <button
              type="button"
              className="glass-button glass-button--ghost"
              onClick={handleLoad}
              style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              <Maximize2 size={13} strokeWidth={1.5} />
              Retry
            </button>
          </>
        )}
        {/* The deep-link fallback always stays available so a CSP block, an
            offline tenant, or an NCBI outage never strands the researcher. */}
        <a
          className="glass-button glass-button--ghost"
          href={fallbackHref}
          target="_blank"
          rel="noopener noreferrer"
          style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
        >
          <ExternalLink size={13} strokeWidth={1.5} />
          Open in new tab
        </a>
      </div>

      {/* The container must be in the DOM before ``SeqView.App(divId)`` runs, so
          it renders as soon as loading starts. NCBI documents a minimum width of
          800px for the pop-up UI; allow horizontal scroll on narrow screens
          rather than clipping the viewer. */}
      {state !== "idle" && (
        <div style={{ overflowX: "auto" }}>
          <div
            id={divIdRef.current}
            style={{
              minWidth: 800,
              minHeight: state === "ready" ? 460 : 0,
              borderRadius: 8,
              background: "rgba(0,0,0,0.10)",
            }}
          />
        </div>
      )}
    </div>
  );
}

export default SViewerEmbed;
