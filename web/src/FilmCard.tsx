import { useCallback, useEffect, useRef, useState } from "react";
import { api, type DemoVideoMeta } from "./api";

const TUCK_KEY = "eddie.filmCard";
// below this stage size the card would crowd the legend and the caption: show the pill instead
const SMALL = "(max-height: 719px), (max-width: 1099px)";

interface Props {
  /** stop every other sound and pause the 3D view: the film has the stage */
  onOpen: () => void;
  onClose: () => void;
}

function stored(): boolean {
  try {
    return window.localStorage.getItem(TUCK_KEY) === "pill";
  } catch {
    return false;
  }
}

/**
 * "A Fistful of Tokens": a poster card at the bottom left of the strip (or a pill, tucked away)
 * that opens the film in a 16:9 lightbox. Local mode plays the MP4 the API streams; youtube mode
 * embeds the public upload. The player exists only while the lightbox is open, so closing it
 * (Escape, the backdrop, the x) stops the sound at once. Never rendered in #cinema: the film
 * would end up recorded inside itself.
 */
export function FilmCard({ onOpen, onClose }: Props) {
  const [meta, setMeta] = useState<DemoVideoMeta | null>(null);
  const [tucked, setTucked] = useState(stored);
  const [small, setSmall] = useState(() => window.matchMedia?.(SMALL).matches ?? false);
  const [open, setOpen] = useState(false);
  const [posterOk, setPosterOk] = useState(true);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const openerRef = useRef<HTMLButtonElement | null>(null);
  const resumeAt = useRef(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const openRef = useRef(false);
  openRef.current = open;

  useEffect(() => {
    let live = true;
    api
      .demoVideoMeta()
      .then((m) => live && setMeta(m))
      .catch(() => live && setMeta({ available: false }));
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    const mq = window.matchMedia?.(SMALL);
    if (!mq) return;
    const on = () => setSmall(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  // leaving the page (or switching to #cinema, which unmounts the card) must not leave the 3D view paused
  useEffect(
    () => () => {
      if (openRef.current) onClose();
    },
    [onClose],
  );

  const tuck = (pill: boolean) => {
    setTucked(pill);
    try {
      window.localStorage.setItem(TUCK_KEY, pill ? "pill" : "card");
    } catch {
      /* private mode: the choice lasts for this page only */
    }
  };

  const show = (opener: HTMLButtonElement | null) => {
    openerRef.current = opener;
    onOpen();
    setOpen(true);
  };

  useEffect(() => {
    const d = dialogRef.current;
    if (open && d && !d.open) d.showModal();
  }, [open]);

  const finish = useCallback(() => {
    if (videoRef.current) {
      resumeAt.current = videoRef.current.currentTime;
      videoRef.current.pause();
    }
    setOpen(false);
    onClose();
    openerRef.current?.focus();
  }, [onClose]);

  const close = () => {
    const d = dialogRef.current;
    if (d?.open) d.close(); // fires "close", which finishes
    else finish();
  };

  if (!meta?.available) return null;
  const label = meta.duration_label ? `${meta.duration_label} ` : "";
  const title = meta.title ?? "A Fistful of Tokens";
  const pill = tucked || small;

  return (
    <>
      {pill ? (
        <div className="film-pill" data-testid="film-pill">
          <button type="button" className="film-pill-play" onClick={(e) => show(e.currentTarget)}
            aria-label={`Watch the ${label}film: ${title}`}>
            <span aria-hidden="true">▶</span> Watch {label}Film
          </button>
          {tucked && !small && (
            <button type="button" className="film-pill-expand" onClick={() => tuck(false)} aria-label="Show the film card"
              title="Show the film card">
              <span aria-hidden="true">⌃</span>
            </button>
          )}
        </div>
      ) : (
        <div className="film-card" data-testid="film-card">
          <button type="button" className="film-open" onClick={(e) => show(e.currentTarget)}
            aria-label={`Play the ${label}film: ${title}`}>
            {meta.poster_src && posterOk ? (
              <img src={meta.poster_src} alt="" loading="lazy" decoding="async" onError={() => setPosterOk(false)} />
            ) : (
              <span className="film-noposter">{title}</span>
            )}
            <span className="film-play" aria-hidden="true">
              <svg width="44" height="44" viewBox="0 0 44 44">
                <circle cx="22" cy="22" r="20" />
                <path d="M 18 14 L 31 22 L 18 30 Z" />
              </svg>
            </span>
          </button>
          <p className="film-caption">
            <b>{title.toUpperCase()}</b>
            <span>
              {" "}
              · {label}Film{meta.cut === "draft" ? " · draft cut" : ""}
            </span>
          </p>
          <button type="button" className="film-tuck" onClick={() => tuck(true)} aria-label="Tuck the film card away"
            title="Tuck away">
            ×
          </button>
        </div>
      )}
      <dialog ref={dialogRef} className="film-modal" data-testid="film-modal" aria-label={`${title}, the ${label}Eddie film`}
        onClose={finish} onClick={(e) => e.target === dialogRef.current && close()}>
        {open && (
          <div className="film-frame">
            <header className="film-head">
              <b>{title}</b>
              <span>
                {label}
                {meta.cut === "draft" ? "· draft cut" : ""}
              </span>
              {meta.mode === "youtube" && meta.watch_url && (
                <a href={meta.watch_url} target="_blank" rel="noopener" className="film-out">
                  Open on YouTube ↗
                </a>
              )}
              <button type="button" className="film-close" onClick={close} aria-label="Close the film" autoFocus>
                ×
              </button>
            </header>
            <div className="film-stage">
              {meta.mode === "youtube" && meta.embed_src ? (
                <iframe src={meta.embed_src} title={title} data-testid="film-iframe"
                  allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowFullScreen
                  referrerPolicy="strict-origin-when-cross-origin" />
              ) : (
                <video ref={videoRef} src={meta.src} poster={meta.poster_src ?? undefined} data-testid="film-video" controls autoPlay
                  playsInline preload="metadata"
                  onLoadedMetadata={(e) => {
                    if (resumeAt.current > 0 && resumeAt.current < e.currentTarget.duration - 1) e.currentTarget.currentTime = resumeAt.current;
                  }} />
              )}
            </div>
          </div>
        )}
      </dialog>
    </>
  );
}
