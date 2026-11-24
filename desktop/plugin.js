/**
 * Talk with Hermes, Desktop half: a Parler page in the sidebar. The call lives
 * at plugin level, so it keeps running while the user goes elsewhere; the
 * status bar shows it and brings the user back.
 *
 * The renderer never talks to Google. The microphone is captured here, PCM
 * goes to our backend (ctx.rest), which owns the Gemini socket and the key.
 * Delegations run here, on a Hermes session of the user's own profile, so
 * every tool goes through the app's normal approvals.
 *
 * Pattern adapted from cruzlxyz/livetalk (MIT), see NOTICE.md.
 * Disk plugin rules: no JSX, only @hermes/plugin-sdk, react and
 * react/jsx-runtime resolve, timers go through ctx so they die with us.
 */

import {
  atom,
  Button,
  host,
  Input,
  PALETTE_AREA,
  pluginSettingsHref,
  ROUTES_AREA,
  SegmentedControl,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  usePluginI18n,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

export const ID = 'talk-with-hermes'
const SESSION_TITLE = 'Appel vocal'
const ROUTE = '/talk'
const POLL_MS = 250
const IN_RATE = 16000
const OUT_RATE = 24000
const MAX_AUDIO_BACKLOG = 4
//: Lines of the previous call kept for "Reprendre", and sent back to the voice.
const RESUME_LINES = 16
//: Played audio starts this far ahead of the clock, and restarts this far
//: ahead after a gap: a late poll eats into it instead of into the voice.
const PLAY_LEAD_S = 0.12
const ANSWER_TIMEOUT_MS = 600000 // same as the backend; Gemini holds the turn open meanwhile
/** A step longer than this is flagged slow on the page. */
const SLOW_STEP_MS = 20000
const BARGE_PEAK = 0.22
const BARGE_BLOCKS = 3
/** Delegations queued at once; more is a loop, not a conversation. */
const MAX_PENDING = 3

/* The orb's colours: one rule set, shared by the Parler page and the module,
 * so the detached window cannot drift from the page. */
export const ORB_CSS = `
.twh-orb{--hue:var(--ui-accent,var(--accent));color:var(--hue);display:block}
.twh-orb[data-state=speaking]{--hue:var(--ui-success,var(--ui-accent,var(--accent)))}
.twh-orb[data-state=thinking]{--hue:var(--ui-warning,var(--ui-accent,var(--accent)))}
.twh-orb[data-state=error]{--hue:var(--ui-danger,var(--ui-text-secondary))}`

const CSS = `
.twh-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ui-text-quaternary)}
.twh-dot[data-state=connecting]{background:var(--ui-text-tertiary);animation:twh-pulse 1s ease-in-out infinite}
.twh-dot[data-state=listening]{background:var(--ui-accent,var(--accent))}
.twh-dot[data-state=speaking]{background:var(--ui-success,var(--ui-accent,var(--accent)))}
.twh-dot[data-state=thinking]{background:var(--ui-warning,var(--ui-text-secondary));animation:twh-pulse .7s ease-in-out infinite}
.twh-dot[data-state=error]{background:var(--ui-danger,var(--ui-text-secondary))}
@keyframes twh-pulse{50%{opacity:.35}}
@keyframes twh-in{from{opacity:0;transform:translateY(6px)}}
@keyframes twh-shimmer{to{background-position:-200% 0}}
.twh-chip{display:inline-flex;align-items:center;gap:6px;background:none;border:0;padding:0 6px;color:inherit;font:inherit;cursor:pointer}
.twh-page{position:relative;height:100%;min-height:0;display:flex;flex-direction:column;align-items:center;overflow:hidden;color:var(--ui-text-primary);padding:20px 16px 12px;box-sizing:border-box}
.twh-page[data-empty]{justify-content:center}
.twh-top{position:absolute;top:12px;right:14px;z-index:2}
.twh-stage{flex:none;display:flex;flex-direction:column;align-items:center;gap:12px}
.twh-page[data-empty] .twh-stage{margin-top:-4vh}`
  + ORB_CSS + `
.twh-orb{width:min(300px,38vh);aspect-ratio:1}
.twh-orb canvas{width:100%;height:100%;display:block}
.twh-state{font-size:15px;font-weight:600;min-height:20px}
.twh-sub{font-size:12px;color:var(--ui-text-secondary);text-align:center;max-width:440px;min-height:16px}
.twh-orbwrap{position:relative;display:flex;justify-content:center}
.twh-side{position:absolute;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:10px;width:min(230px,calc((100cqi - min(300px,38vh)) / 2 - 24px))}
.twh-side[data-side=left]{right:calc(100% + 10px);align-items:flex-end}
.twh-side[data-side=right]{left:calc(100% + 10px);align-items:flex-start}
.twh-page{container-type:inline-size}
@container (max-width:720px){.twh-side{position:static;transform:none;width:auto;max-width:100%;align-items:center}.twh-orbwrap{flex-direction:column;align-items:center}.twh-link{display:none}}
.twh-step{--tone:var(--ui-warning,var(--ui-accent));position:relative;display:flex;align-items:center;gap:8px;max-width:100%;padding:6px 10px 6px 6px;border-radius:12px;font-size:12px;line-height:16px;border:1px solid color-mix(in srgb,var(--tone) 55%,transparent);background:color-mix(in srgb,var(--tone) 9%,var(--ui-bg-card,transparent));color:var(--ui-text-primary);box-shadow:0 4px 14px color-mix(in srgb,var(--tone) 16%,transparent);animation:twh-pop .35s cubic-bezier(.2,1.4,.4,1)}
.twh-side[data-side=left] .twh-step{flex-direction:row-reverse;padding:6px 6px 6px 10px;text-align:right}
.twh-step[data-done=true]{--tone:var(--ui-success,var(--ui-accent));opacity:.8;box-shadow:none}
.twh-step[data-slow=true]{--tone:var(--ui-danger,var(--ui-warning))}
.twh-step b{display:block;font-weight:600}
.twh-step small{display:block;color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:170px}
.twh-step .twh-txt{min-width:0}
.twh-ico{flex:none;width:30px;height:30px;border-radius:9px;display:grid;place-items:center;color:var(--tone);background:color-mix(in srgb,var(--tone) 16%,transparent)}
.twh-ico svg{width:20px;height:20px;overflow:visible;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.twh-link{position:absolute;top:50%;width:14px;height:2px;background:repeating-linear-gradient(90deg,var(--tone) 0 3px,transparent 3px 6px);background-size:12px 2px;animation:twh-flow .6s linear infinite}
.twh-side[data-side=right] .twh-link{right:100%;animation-direction:reverse}
.twh-side[data-side=left] .twh-link{left:100%}
.twh-step[data-done=true] .twh-link{animation:none;opacity:.4}
@keyframes twh-pop{from{opacity:0;transform:scale(.6)}}
@keyframes twh-flow{to{background-position:12px 0}}
@keyframes twh-spin{to{transform:rotate(360deg)}}
@keyframes twh-meridian{50%{transform:scaleX(.15)}}
@keyframes twh-blink{50%{opacity:0}}
@keyframes twh-scan{0%{transform:translateY(-5px)}50%{transform:translateY(5px)}100%{transform:translateY(-5px)}}
@keyframes twh-write{0%,100%{transform:translate(-3px,0)}50%{transform:translate(3px,-1px)}}
@keyframes twh-orbit{to{transform:rotate(360deg)}}
@keyframes twh-fill{0%{transform:scaleX(.05)}100%{transform:scaleX(1)}}
@keyframes twh-beat{50%{transform:scale(1.18)}}
@keyframes twh-tick{0%,40%{stroke-dashoffset:12}70%,100%{stroke-dashoffset:0}}
.twh-step[data-done=false] .twh-a-spin{transform-origin:12px 12px;animation:twh-spin 2.4s linear infinite}
.twh-step[data-done=false] .twh-a-mer{transform-origin:12px 12px;animation:twh-meridian 1.6s ease-in-out infinite}
.twh-step[data-done=false] .twh-a-blink{animation:twh-blink .9s steps(1) infinite}
.twh-step[data-done=false] .twh-a-scan{animation:twh-scan 1.4s ease-in-out infinite}
.twh-step[data-done=false] .twh-a-write{animation:twh-write .8s ease-in-out infinite}
.twh-step[data-done=false] .twh-a-orbit{transform-origin:12px 12px;animation:twh-orbit 1.2s linear infinite}
.twh-step[data-done=false] .twh-a-fill{transform-origin:5px 0;animation:twh-fill 1.6s ease-in-out infinite}
.twh-step[data-done=false] .twh-a-beat{transform-origin:12px 12px;animation:twh-beat 1s ease-in-out infinite}
.twh-step[data-done=false] .twh-a-tick{stroke-dasharray:12;animation:twh-tick 1.4s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){.twh-step *{animation:none!important}}
.twh-perf{width:100%;max-width:680px;flex:none;font-size:11px;color:var(--ui-text-tertiary)}
.twh-perf summary{cursor:pointer;padding:4px 0}
.twh-perf-row{display:flex;gap:8px;line-height:17px}
.twh-perf-row b{font-variant-numeric:tabular-nums;min-width:64px;color:var(--ui-text-secondary)}
.twh-perf-row[data-slow=true] b{color:var(--ui-danger,var(--ui-text-secondary))}
.twh-feed{flex:1;min-height:0;width:100%;max-width:680px;margin-top:14px;overflow-y:auto;display:flex;flex-direction:column-reverse;border-top:1px solid var(--ui-stroke-secondary);mask-image:linear-gradient(to bottom,transparent,black 28px)}
.twh-feed-inner{display:flex;flex-direction:column;gap:8px;padding:12px 4px}
.twh-msg{max-width:80%;padding:7px 11px;border-radius:14px;font-size:13px;line-height:19px;white-space:pre-wrap;overflow-wrap:anywhere;animation:twh-in .2s ease-out}
.twh-msg[data-who=toi]{align-self:flex-end;background:var(--ui-bg-secondary,var(--ui-bg-card));color:var(--ui-text-primary)}
.twh-msg[data-who=hermes]{align-self:flex-start;background:var(--ui-bg-card);border:1px solid var(--ui-stroke-secondary);color:var(--ui-text-primary)}
.twh-msg[data-who=agent]{align-self:center;max-width:90%;margin:4px 0;padding:2px 8px;font-size:12px;font-style:italic;color:var(--ui-text-secondary)}
.twh-details{width:100%;max-width:680px;flex:none;font-size:11px;color:var(--ui-text-tertiary)}
.twh-details summary{cursor:pointer;padding:4px 0}
.twh-log{font-family:var(--font-mono,monospace);line-height:15px;max-height:120px;overflow-y:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.twh-settings{position:absolute;top:48px;right:14px;bottom:12px;z-index:3;width:min(380px,calc(100% - 28px));display:flex;flex-direction:column;gap:14px;padding:16px;box-sizing:border-box;overflow-y:auto;border-radius:12px;border:1px solid var(--ui-stroke-secondary);background:var(--ui-bg-elevated,var(--ui-bg-editor));box-shadow:0 12px 40px color-mix(in srgb,var(--ui-text-primary) 14%,transparent);animation:twh-in .18s ease-out}
.twh-field{display:flex;flex-direction:column;gap:6px;font-size:12px}
.twh-field > label,.twh-field > .twh-label{font-weight:600;color:var(--ui-text-primary)}
.twh-hint{font-size:11px;color:var(--ui-text-tertiary)}
.twh-words{display:flex;flex-direction:column;gap:6px}
.twh-word{display:grid;grid-template-columns:1fr 1.4fr auto;gap:6px;align-items:center}
.twh-word input{min-width:0}
.twh-x{background:none;border:0;cursor:pointer;color:var(--ui-text-tertiary);font-size:14px;padding:0 4px}
.twh-error{font-size:12px;color:var(--ui-danger,var(--ui-text-secondary))}
.twh-row{display:flex;gap:8px;align-items:center}
.twh-link-row,.twh-link-box{width:100%;max-width:680px;flex:none;display:flex;flex-direction:column;align-items:center;gap:6px;margin-top:10px;font-size:12px;color:var(--ui-text-secondary)}
.twh-link-row{flex-direction:row;justify-content:center}
.twh-sessions{width:100%;max-width:420px;max-height:240px;overflow-y:auto;display:flex;flex-direction:column;gap:2px;padding:4px;border:1px solid var(--ui-stroke-secondary);border-radius:10px;background:var(--ui-bg-card)}
.twh-session{display:flex;justify-content:space-between;gap:10px;padding:6px 8px;border:0;border-radius:7px;background:none;color:var(--ui-text-primary);font:inherit;font-size:12px;text-align:left;cursor:pointer}
.twh-session b{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.twh-session span{flex:none;color:var(--ui-text-tertiary)}
.twh-session:hover,.twh-session:focus-visible{background:color-mix(in srgb,var(--ui-accent,var(--accent)) 12%,transparent);outline:none}
.twh-pip-probe{background-color:var(--ui-bg-elevated,var(--ui-bg-card,var(--ui-bg-editor)));color:var(--ui-text-primary);border-color:var(--ui-text-secondary)}
.twh-top{display:flex;gap:4px}
.twh-voices{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:6px;max-height:212px;overflow-y:auto;padding:2px 2px 18px;mask-image:linear-gradient(to bottom,black calc(100% - 24px),transparent)}
.twh-voice{display:flex;flex-direction:column;align-items:flex-start;gap:1px;padding:6px 8px;border-radius:8px;border:1px solid var(--ui-stroke-secondary);background:none;color:var(--ui-text-primary);font:inherit;font-size:12px;cursor:pointer;text-align:left}
.twh-voice span{font-size:11px;color:var(--ui-text-tertiary)}
.twh-voice[data-on=true]{border-color:var(--ui-accent,var(--accent));background:color-mix(in srgb,var(--ui-accent,var(--accent)) 12%,transparent)}
.twh-voice:focus-visible{outline:2px solid var(--ui-accent,var(--accent));outline-offset:1px}
`

// -- delegation: one request at a time, on the installed agent's profile -----

/** "4,2 s", "3 min 06 s": how a duration reads on the page. */
// -- languages: French, English, Spanish; Desktop's language picks ----------

/** Flat keys, one table per language. `fr` is the source; the host falls
 *  back to `en`, then to the key. A function takes positional arguments. */
const STRINGS = {
  fr: {
    'meta.locale': 'fr', 'meta.decimal': ',',
    'trait.dramatic': 'dramatique', 'trait.elegant': 'élégante', 'trait.confident': 'assurée', 'trait.calm': 'calme', 'trait.precise': 'précise', 'trait.playful': 'espiègle', 'trait.bright': 'lumineuse', 'trait.upbeat': 'enjouée', 'trait.informative': 'informative', 'trait.firm': 'ferme', 'trait.excitable': 'vive', 'trait.youthful': 'jeune', 'trait.breezy': 'légère', 'trait.easygoing': 'détendue', 'trait.breathy': 'soufflée', 'trait.clear': 'claire', 'trait.smooth': 'veloutée', 'trait.gravelly': 'rocailleuse', 'trait.soft': 'douce', 'trait.even': 'posée', 'trait.mature': 'mûre', 'trait.forward': 'affirmée', 'trait.friendly': 'amicale', 'trait.casual': 'décontractée', 'trait.gentle': 'bienveillante', 'trait.lively': "pleine d'entrain", 'trait.knowledgeable': 'savante', 'trait.warm': 'chaleureuse',
    'tool.files': 'Fichiers', 'tool.search': 'Recherche', 'tool.write': 'Écriture', 'tool.browser': 'Navigateur', 'tool.agents': 'Sous-agent', 'tool.memory': 'Mémoire', 'tool.skill': 'Compétence', 'tool.clock': 'Planification', 'tool.tool': 'Outil',
    'state.idle': 'Prêt', 'state.connecting': 'Connexion…', 'state.listening': "J'écoute", 'state.speaking': 'Je parle',
    'state.thinking': 'Je travaille', 'state.error': 'Erreur',
    'shape.circle': 'Rond', 'shape.square': 'Carré', 'shape.oval': 'Ovale', 'shape.triangle': 'Triangle',
    'nav.talk': 'Parler', 'palette.open': 'Parler à mon agent',
    'plugin.description': "Parle à ton agent en direct, depuis l'onglet Parler.",
    'page.talkTo': n => `Parler à ${n}`, 'page.yourAgent': 'ton agent', 'page.YourAgent': 'Ton agent',
    'sub.muted': "Micro coupé : je ne t'entends plus.",
    'sub.listen.tu': "Parle, je t'écoute. Tu peux me couper.", 'sub.listen.vous': 'Parlez, je vous écoute. Vous pouvez me couper.',
    'sub.thinking': n => `${n} est sur le coup.`, 'sub.idle': "Une fois lancé, l'appel continue si tu changes de page.",
    'key.needed': (svc = 'Gemini Live') => svc === 'Gemini Live' ? 'Il faut une clé Gemini pour parler. Elle se crée gratuitement sur aistudio.google.com, puis se colle dans les réglages du plugin.' : `Il faut une clé ${svc} pour parler. Elle se colle dans les réglages du plugin.`,
    'key.add': (svc = 'Gemini Live') => `Ajouter la clé ${svc}`,
    'btn.detach': 'Détacher', 'btn.attach': 'Rattacher', 'btn.detach.title': 'Une petite fenêtre au-dessus de tout, à poser où tu veux',
    'btn.clear': 'Effacer', 'btn.clear.title': "Vide l'écran. L'agent garde le fil.",
    'btn.hideChat': 'Masquer le chat', 'btn.showChat': 'Afficher le chat', 'btn.settings': 'Réglages',
    'btn.resume': 'Reprendre la conversation', 'btn.resume.title': (n, when) => `${n} répliques, ${when}`,
    'btn.hangup': 'Raccrocher', 'btn.retry': 'Réessayer', 'btn.newCall': 'Nouvel appel', 'btn.start': "Démarrer l'appel",
    'btn.mute': 'Couper le micro', 'btn.unmute': 'Rouvrir le micro',
    'msg.agent': x => `Agent : ${x}`, 'details': 'Détails techniques', 'log.aria': 'journal', 'steps.aria': "Ce que fait l'agent",
    'step.slow': ', lent',
    'perf.summary': (n, avg, cuts) => `Performances : ${n} demande${n > 1 ? 's' : ''}${avg ? `, ${avg} en moyenne` : ''}${cuts ? `, ${cuts} coupure${cuts > 1 ? 's' : ''} du son` : ''}`,
    'perf.failed': ms => `échec ${ms}`, 'perf.slowest': (label, ms) => ` · le plus long : ${label} ${ms}`,
    'link.session': x => `Session : ${x}`, 'link.unlink': 'Délier', 'link.open': 'Lier à une session', 'link.close': 'Fermer la liste',
    'link.aria': 'Sessions', 'link.unavailable': e => `Liste indisponible : ${e}`, 'link.loading': 'Chargement…',
    'link.none': 'Aucune session pour cet agent.',
    'chip.title': "Revenir à l'appel", 'chip.label': x => `Appel · ${x}`,
    'settings.aria': 'Réglages de Parler', 'settings.title': 'Réglages', 'settings.close': 'Fermer',
    'settings.shape': 'Forme', 'settings.shape.hint': 'Change tout de suite.',
    'settings.address': n => `${n} te parle en`, 'settings.address.tu': 'Tutoiement', 'settings.address.vous': 'Vouvoiement',
    'settings.voice': 'Voix', 'settings.voice.hint': (n, s) => `Les ${n} voix de ${s}. Prise en compte au prochain appel.`,
    'settings.service': 'ce service',
    'settings.dict': 'Dictionnaire', 'settings.dict.hint': "Les mots que la voix entend mal. Ils sont corrigés dans la conversation et dans ce que reçoit l'agent.",
    'settings.dict.word': 'Mot juste', 'settings.dict.heard': 'Entendu comme (virgules)', 'settings.dict.remove': 'Retirer',
    'settings.dict.add': 'Ajouter un mot', 'settings.save': 'Enregistrer', 'settings.saved': 'Enregistré. Pris en compte au prochain appel.',
    'settings.footer': 'Tutoiement, voix et dictionnaire : pris en compte au prochain appel.',
    'err.micDenied': 'Micro refusé. Autorise le micro pour Hermes dans les réglages du système.',
    'err.takenOver': "L'appel a été repris dans une autre fenêtre.", 'err.ended': 'Appel terminé.',
    'err.startFailed': 'Démarrage impossible.', 'err.noPip': "La fenêtre flottante n'est pas disponible ici.",
    'note.looking': x => `je cherche : ${x}`, 'note.answered': (ms, x) => `répondu en ${ms} : ${x}`,
    'note.timeout': ms => `pas de réponse à temps (${ms})`, 'note.failed': x => `échec : ${x}`,
    'log.profile': p => `agent : profil ${p}`, 'log.noProfile': p => `profil ${p} introuvable, demandes envoyées au profil actif`,
    'log.linkedSession': x => `session liée ${x}`, 'log.voiceSession': x => `session vocale ${x}`,
    'log.refused': e => `envoi refusé (${e}), nouvelle session`, 'log.cancelled': "annulé, arrêt du tour de l'agent",
    'log.timeout': "délai dépassé, arrêt du tour de l'agent", 'log.hungUp': "appel terminé, arrêt du tour de l'agent",
    'log.delegationFailed': e => `délégation échouée : ${e}`, 'log.tooMany': 'trop de demandes en attente, refusée',
    'log.delegation': x => `délégation : ${x}`,
    'log.underrun': (n, ms) => `coupure du son : ${n} trou${n > 1 ? 's' : ''} côté haut-parleur, ${ms} ms au total`,
    'log.dry': (n, list) => `coupure du son : la file s'est vidée ${n} fois (${list} ms)`,
    'log.muted': 'micro coupé : périphérique libéré, la voix prévenue', 'log.unmuted': 'micro rouvert',
    'log.micUnavailable': e => `micro indisponible : ${e}`,
    'log.barge': "réponse coupée : ta voix (ou l'écho du haut-parleur) a été entendue",
    'log.interrupted': "La voix a arrêté sa réponse : elle a cru t'entendre",
    'log.contextLost': 'reprise impossible, la conversation repart à zéro', 'log.closed': x => `fin : ${x || 'coupure'}`,
    'log.linked': (t, n) => `liée à la session « ${t} », ${n} messages repris`,
    'log.linkFailed': (t, e) => `session « ${t} » illisible (${e}), appel sans elle`,
    'log.connecting': (svc = 'Gemini Live') => `connexion à ${svc}…`, 'log.resumed': n => `reprise de l'appel précédent, ${n} répliques rappelées`,
    'log.startRefused': x => `refus : ${x}`, 'log.online': m => `en ligne, ${m}`, 'log.error': e => `erreur : ${e}`,
    'log.linkSet': t => `appel lié à « ${t} » au prochain démarrage`, 'log.linkCleared': 'appel délié de la session'
  },
  en: {
    'meta.locale': 'en', 'meta.decimal': '.',
    'trait.dramatic': 'Dramatic', 'trait.elegant': 'Elegant', 'trait.confident': 'Confident', 'trait.calm': 'Calm', 'trait.precise': 'Precise', 'trait.playful': 'Playful', 'trait.bright': 'Bright', 'trait.upbeat': 'Upbeat', 'trait.informative': 'Informative', 'trait.firm': 'Firm', 'trait.excitable': 'Excitable', 'trait.youthful': 'Youthful', 'trait.breezy': 'Breezy', 'trait.easygoing': 'Easy-going', 'trait.breathy': 'Breathy', 'trait.clear': 'Clear', 'trait.smooth': 'Smooth', 'trait.gravelly': 'Gravelly', 'trait.soft': 'Soft', 'trait.even': 'Even', 'trait.mature': 'Mature', 'trait.forward': 'Forward', 'trait.friendly': 'Friendly', 'trait.casual': 'Casual', 'trait.gentle': 'Gentle', 'trait.lively': 'Lively', 'trait.knowledgeable': 'Knowledgeable', 'trait.warm': 'Warm',
    'tool.files': 'Files', 'tool.search': 'Search', 'tool.write': 'Writing', 'tool.browser': 'Browser', 'tool.agents': 'Sub-agent', 'tool.memory': 'Memory', 'tool.skill': 'Skill', 'tool.clock': 'Scheduling', 'tool.tool': 'Tool',
    'state.idle': 'Ready', 'state.connecting': 'Connecting…', 'state.listening': 'Listening', 'state.speaking': 'Speaking',
    'state.thinking': 'Working', 'state.error': 'Error',
    'shape.circle': 'Round', 'shape.square': 'Square', 'shape.oval': 'Oval', 'shape.triangle': 'Triangle',
    'nav.talk': 'Talk', 'palette.open': 'Talk to my agent',
    'plugin.description': 'Talk to your agent live, from the Talk tab.',
    'page.talkTo': n => `Talk to ${n}`, 'page.yourAgent': 'your agent', 'page.YourAgent': 'Your agent',
    'sub.muted': "Mic off: I can't hear you.",
    'sub.listen.tu': "Go ahead, I'm listening. You can interrupt me.", 'sub.listen.vous': "Go ahead, I'm listening. You can interrupt me.",
    'sub.thinking': n => `${n} is on it.`, 'sub.idle': 'Once started, the call keeps going if you switch pages.',
    'key.needed': (svc = 'Gemini Live') => svc === 'Gemini Live' ? 'You need a Gemini key to talk. Create one for free at aistudio.google.com, then paste it in the plugin settings.' : `You need a ${svc} key to talk. Paste it in the plugin settings.`,
    'key.add': (svc = 'Gemini Live') => `Add the ${svc} key`,
    'btn.detach': 'Pop out', 'btn.attach': 'Dock', 'btn.detach.title': 'A small window above everything, place it wherever you like',
    'btn.clear': 'Clear', 'btn.clear.title': 'Clears the screen. The agent keeps the thread.',
    'btn.hideChat': 'Hide chat', 'btn.showChat': 'Show chat', 'btn.settings': 'Settings',
    'btn.resume': 'Resume conversation', 'btn.resume.title': (n, when) => `${n} lines, ${when}`,
    'btn.hangup': 'Hang up', 'btn.retry': 'Retry', 'btn.newCall': 'New call', 'btn.start': 'Start the call',
    'btn.mute': 'Mute mic', 'btn.unmute': 'Unmute mic',
    'msg.agent': x => `Agent: ${x}`, 'details': 'Technical details', 'log.aria': 'log', 'steps.aria': 'What the agent is doing',
    'step.slow': ', slow',
    'perf.summary': (n, avg, cuts) => `Performance: ${n} request${n > 1 ? 's' : ''}${avg ? `, ${avg} on average` : ''}${cuts ? `, ${cuts} audio dropout${cuts > 1 ? 's' : ''}` : ''}`,
    'perf.failed': ms => `failed ${ms}`, 'perf.slowest': (label, ms) => ` · slowest: ${label} ${ms}`,
    'link.session': x => `Session: ${x}`, 'link.unlink': 'Unlink', 'link.open': 'Link to a session', 'link.close': 'Close the list',
    'link.aria': 'Sessions', 'link.unavailable': e => `List unavailable: ${e}`, 'link.loading': 'Loading…',
    'link.none': 'No session for this agent.',
    'chip.title': 'Back to the call', 'chip.label': x => `Call · ${x}`,
    'settings.aria': 'Talk settings', 'settings.title': 'Settings', 'settings.close': 'Close',
    'settings.shape': 'Shape', 'settings.shape.hint': 'Applies right away.',
    'settings.address': n => `How ${n} addresses you (French, Spanish)`, 'settings.address.tu': 'Casual', 'settings.address.vous': 'Formal',
    'settings.voice': 'Voice', 'settings.voice.hint': (n, s) => `The ${n} voices of ${s}. Used from the next call.`,
    'settings.service': 'this service',
    'settings.dict': 'Dictionary', 'settings.dict.hint': 'Words the voice mishears. They are fixed in the conversation and in what the agent receives.',
    'settings.dict.word': 'Correct word', 'settings.dict.heard': 'Heard as (commas)', 'settings.dict.remove': 'Remove',
    'settings.dict.add': 'Add a word', 'settings.save': 'Save', 'settings.saved': 'Saved. Used from the next call.',
    'settings.footer': 'Address, voice and dictionary: used from the next call.',
    'err.micDenied': 'Microphone denied. Allow the microphone for Hermes in the system settings.',
    'err.takenOver': 'The call was taken over in another window.', 'err.ended': 'Call ended.',
    'err.startFailed': 'Could not start.', 'err.noPip': 'The floating window is not available here.',
    'note.looking': x => `looking into: ${x}`, 'note.answered': (ms, x) => `answered in ${ms}: ${x}`,
    'note.timeout': ms => `no answer in time (${ms})`, 'note.failed': x => `failed: ${x}`,
    'log.profile': p => `agent: profile ${p}`, 'log.noProfile': p => `profile ${p} not found, requests go to the active profile`,
    'log.linkedSession': x => `linked session ${x}`, 'log.voiceSession': x => `voice session ${x}`,
    'log.refused': e => `send refused (${e}), new session`, 'log.cancelled': "cancelled, stopping the agent's turn",
    'log.timeout': "timed out, stopping the agent's turn", 'log.hungUp': "call ended, stopping the agent's turn",
    'log.delegationFailed': e => `delegation failed: ${e}`, 'log.tooMany': 'too many pending requests, refused',
    'log.delegation': x => `delegation: ${x}`,
    'log.underrun': (n, ms) => `audio dropout: ${n} gap${n > 1 ? 's' : ''} on the speaker, ${ms} ms in total`,
    'log.dry': (n, list) => `audio dropout: the queue ran dry ${n} time${n > 1 ? 's' : ''} (${list} ms)`,
    'log.muted': 'mic off: device released, the voice told', 'log.unmuted': 'mic on',
    'log.micUnavailable': e => `mic unavailable: ${e}`,
    'log.barge': 'answer cut: your voice (or speaker echo) was heard',
    'log.interrupted': 'The voice stopped its answer: it thought it heard you',
    'log.contextLost': 'could not resume, the conversation starts over', 'log.closed': x => `ended: ${x || 'cut'}`,
    'log.linked': (t, n) => `linked to the session "${t}", ${n} messages recalled`,
    'log.linkFailed': (t, e) => `session "${t}" unreadable (${e}), call without it`,
    'log.connecting': (svc = 'Gemini Live') => `connecting to ${svc}…`, 'log.resumed': n => `resuming the previous call, ${n} lines recalled`,
    'log.startRefused': x => `refused: ${x}`, 'log.online': m => `online, ${m}`, 'log.error': e => `error: ${e}`,
    'log.linkSet': t => `call linked to "${t}" from the next start`, 'log.linkCleared': 'call unlinked from the session'
  },
  es: {
    'meta.locale': 'es', 'meta.decimal': ',',
    'trait.dramatic': 'dramática', 'trait.elegant': 'elegante', 'trait.confident': 'segura', 'trait.calm': 'tranquila', 'trait.precise': 'precisa', 'trait.playful': 'juguetona', 'trait.bright': 'luminosa', 'trait.upbeat': 'alegre', 'trait.informative': 'informativa', 'trait.firm': 'firme', 'trait.excitable': 'vivaz', 'trait.youthful': 'juvenil', 'trait.breezy': 'ligera', 'trait.easygoing': 'relajada', 'trait.breathy': 'aireada', 'trait.clear': 'clara', 'trait.smooth': 'suave', 'trait.gravelly': 'ronca', 'trait.soft': 'dulce', 'trait.even': 'serena', 'trait.mature': 'madura', 'trait.forward': 'decidida', 'trait.friendly': 'amable', 'trait.casual': 'desenfadada', 'trait.gentle': 'amable y suave', 'trait.lively': 'animada', 'trait.knowledgeable': 'experta', 'trait.warm': 'cálida',
    'tool.files': 'Archivos', 'tool.search': 'Búsqueda', 'tool.write': 'Escritura', 'tool.browser': 'Navegador', 'tool.agents': 'Subagente', 'tool.memory': 'Memoria', 'tool.skill': 'Habilidad', 'tool.clock': 'Planificación', 'tool.tool': 'Herramienta',
    'state.idle': 'Listo', 'state.connecting': 'Conectando…', 'state.listening': 'Te escucho', 'state.speaking': 'Hablando',
    'state.thinking': 'Trabajando', 'state.error': 'Error',
    'shape.circle': 'Redondo', 'shape.square': 'Cuadrado', 'shape.oval': 'Óvalo', 'shape.triangle': 'Triángulo',
    'nav.talk': 'Hablar', 'palette.open': 'Hablar con mi agente',
    'plugin.description': 'Habla con tu agente en directo, desde la pestaña Hablar.',
    'page.talkTo': n => `Hablar con ${n}`, 'page.yourAgent': 'tu agente', 'page.YourAgent': 'Tu agente',
    'sub.muted': 'Micrófono apagado: ya no te oigo.',
    'sub.listen.tu': 'Habla, te escucho. Puedes interrumpirme.', 'sub.listen.vous': 'Hable, le escucho. Puede interrumpirme.',
    'sub.thinking': n => `${n} se está encargando.`, 'sub.idle': 'Una vez iniciada, la llamada sigue aunque cambies de página.',
    'key.needed': (svc = 'Gemini Live') => svc === 'Gemini Live' ? 'Necesitas una clave de Gemini para hablar. Créala gratis en aistudio.google.com y pégala en los ajustes del plugin.' : `Necesitas una clave de ${svc} para hablar. Pégala en los ajustes del plugin.`,
    'key.add': (svc = 'Gemini Live') => `Añadir la clave de ${svc}`,
    'btn.detach': 'Desacoplar', 'btn.attach': 'Acoplar', 'btn.detach.title': 'Una ventana pequeña encima de todo, colócala donde quieras',
    'btn.clear': 'Borrar', 'btn.clear.title': 'Vacía la pantalla. El agente conserva el hilo.',
    'btn.hideChat': 'Ocultar chat', 'btn.showChat': 'Mostrar chat', 'btn.settings': 'Ajustes',
    'btn.resume': 'Retomar la conversación', 'btn.resume.title': (n, when) => `${n} intervenciones, ${when}`,
    'btn.hangup': 'Colgar', 'btn.retry': 'Reintentar', 'btn.newCall': 'Nueva llamada', 'btn.start': 'Iniciar la llamada',
    'btn.mute': 'Silenciar micrófono', 'btn.unmute': 'Activar micrófono',
    'msg.agent': x => `Agente: ${x}`, 'details': 'Detalles técnicos', 'log.aria': 'registro', 'steps.aria': 'Lo que hace el agente',
    'step.slow': ', lento',
    'perf.summary': (n, avg, cuts) => `Rendimiento: ${n} petición${n > 1 ? 'es' : ''}${avg ? `, ${avg} de media` : ''}${cuts ? `, ${cuts} corte${cuts > 1 ? 's' : ''} de sonido` : ''}`,
    'perf.failed': ms => `fallo ${ms}`, 'perf.slowest': (label, ms) => ` · lo más lento: ${label} ${ms}`,
    'link.session': x => `Sesión: ${x}`, 'link.unlink': 'Desvincular', 'link.open': 'Vincular a una sesión', 'link.close': 'Cerrar la lista',
    'link.aria': 'Sesiones', 'link.unavailable': e => `Lista no disponible: ${e}`, 'link.loading': 'Cargando…',
    'link.none': 'No hay sesiones para este agente.',
    'chip.title': 'Volver a la llamada', 'chip.label': x => `Llamada · ${x}`,
    'settings.aria': 'Ajustes de Hablar', 'settings.title': 'Ajustes', 'settings.close': 'Cerrar',
    'settings.shape': 'Forma', 'settings.shape.hint': 'Se aplica al momento.',
    'settings.address': n => `Cómo te trata ${n}`, 'settings.address.tu': 'Tú', 'settings.address.vous': 'Usted',
    'settings.voice': 'Voz', 'settings.voice.hint': (n, s) => `Las ${n} voces de ${s}. Se aplica en la próxima llamada.`,
    'settings.service': 'este servicio',
    'settings.dict': 'Diccionario', 'settings.dict.hint': 'Palabras que la voz entiende mal. Se corrigen en la conversación y en lo que recibe el agente.',
    'settings.dict.word': 'Palabra correcta', 'settings.dict.heard': 'Oída como (comas)', 'settings.dict.remove': 'Quitar',
    'settings.dict.add': 'Añadir una palabra', 'settings.save': 'Guardar', 'settings.saved': 'Guardado. Se aplica en la próxima llamada.',
    'settings.footer': 'Trato, voz y diccionario: se aplican en la próxima llamada.',
    'err.micDenied': 'Micrófono denegado. Permite el micrófono para Hermes en los ajustes del sistema.',
    'err.takenOver': 'La llamada se retomó en otra ventana.', 'err.ended': 'Llamada terminada.',
    'err.startFailed': 'No se pudo iniciar.', 'err.noPip': 'La ventana flotante no está disponible aquí.',
    'note.looking': x => `buscando: ${x}`, 'note.answered': (ms, x) => `respondido en ${ms}: ${x}`,
    'note.timeout': ms => `sin respuesta a tiempo (${ms})`, 'note.failed': x => `fallo: ${x}`,
    'log.profile': p => `agente: perfil ${p}`, 'log.noProfile': p => `perfil ${p} no encontrado, las peticiones van al perfil activo`,
    'log.linkedSession': x => `sesión vinculada ${x}`, 'log.voiceSession': x => `sesión de voz ${x}`,
    'log.refused': e => `envío rechazado (${e}), nueva sesión`, 'log.cancelled': 'cancelado, se detiene el turno del agente',
    'log.timeout': 'tiempo agotado, se detiene el turno del agente', 'log.hungUp': 'llamada terminada, se detiene el turno del agente',
    'log.delegationFailed': e => `delegación fallida: ${e}`, 'log.tooMany': 'demasiadas peticiones pendientes, rechazada',
    'log.delegation': x => `delegación: ${x}`,
    'log.underrun': (n, ms) => `corte de sonido: ${n} hueco${n > 1 ? 's' : ''} en el altavoz, ${ms} ms en total`,
    'log.dry': (n, list) => `corte de sonido: la cola se vació ${n} ${n > 1 ? 'veces' : 'vez'} (${list} ms)`,
    'log.muted': 'micrófono apagado: dispositivo liberado, la voz avisada', 'log.unmuted': 'micrófono activado',
    'log.micUnavailable': e => `micrófono no disponible: ${e}`,
    'log.barge': 'respuesta cortada: se oyó tu voz (o el eco del altavoz)',
    'log.interrupted': 'La voz paró su respuesta: creyó oírte',
    'log.contextLost': 'no se pudo retomar, la conversación vuelve a empezar', 'log.closed': x => `fin: ${x || 'corte'}`,
    'log.linked': (t, n) => `vinculada a la sesión «${t}», ${n} mensajes recuperados`,
    'log.linkFailed': (t, e) => `sesión «${t}» ilegible (${e}), llamada sin ella`,
    'log.connecting': (svc = 'Gemini Live') => `conectando con ${svc}…`, 'log.resumed': n => `retomando la llamada anterior, ${n} intervenciones recordadas`,
    'log.startRefused': x => `rechazo: ${x}`, 'log.online': m => `en línea, ${m}`, 'log.error': e => `error: ${e}`,
    'log.linkSet': t => `llamada vinculada a «${t}» en el próximo inicio`, 'log.linkCleared': 'llamada desvinculada de la sesión'
  }
}

/** Dotted flat keys into the nested tree the host walks. */
export function nest(flat) {
  const out = {}
  for (const [key, value] of Object.entries(flat)) {
    const parts = key.split('.')
    let node = out
    for (const part of parts.slice(0, -1)) {
      // a leaf and a branch may share a name ('btn.detach', 'btn.detach.title'): the leaf moves to '_'
      if (typeof node[part] !== 'object') node[part] = node[part] === undefined ? {} : { _: node[part] }
      node = node[part]
    }
    const last = parts[parts.length - 1]
    if (typeof node[last] === 'object') node[last]._ = value
    else node[last] = value
  }
  return out
}

/** The plugin's bundles, in the host's shape. */
export const BUNDLES = Object.fromEntries(Object.entries(STRINGS).map(([lang, flat]) => [lang, nest(flat)]))

/** A key whose leaf moved under '_' (see nest) is asked for with '._'. */
export function keyPath(key, flat = STRINGS.fr) {
  return Object.keys(flat).some(k => k.startsWith(`${key}.`)) ? `${key}._` : key
}

/** Same lookup as the host, used before registration and in tests: French. */
function localT(key, ...args) {
  const v = STRINGS.fr[key] ?? STRINGS.en[key]
  return typeof v === 'function' ? v(...args) : v ?? key
}

let hostT = null
/** Module-level translator: Desktop's language once registered, French before. */
export function T(key, ...args) {
  return hostT ? hostT(keyPath(key), ...args) : localT(key, ...args)
}

/** React: re-renders on a language switch. */
function useT() {
  const t = usePluginI18n(ID)
  return (key, ...args) => t(keyPath(key), ...args)
}

export function formatMs(ms) {
  const s = Math.max(0, ms) / 1000
  if (s < 60) return `${s.toFixed(1).replace('.', T('meta.decimal'))} s`
  const m = Math.floor(s / 60)
  return `${m} min ${String(Math.round(s - m * 60)).padStart(2, '0')} s`
}

/**
 * RPC on the profile the plugin is installed on, whatever profile the window
 * shows. Live 2026-11-21: delegations ran on the active (default) profile,
 * another agent with another model and other tools. The route comes from the
 * connection registry; without one, the active gateway, said in the log.
 */
export function createProfileRequest({ host, profile, log }) {
  let route = null, resolved = false, release = null
  async function resolve() {
    if (resolved) return route
    resolved = true
    const want = profile()
    try {
      const routes = (await host.profileRoutes?.()) || []
      const active = host.activeConnectionId?.() || null
      const match = routes.filter(r => r && (r.targetProfile === want || r.profile === want))
      route = match.find(r => r.connectionId === active) || match[0] || null
    } catch { route = null }
    if (route && typeof host.retainProfile === 'function') {
      try { release = await host.retainProfile(route, { spawnPriority: 'foreground' }) } catch { release = null }
    }
    log(route ? T('log.profile', want) : T('log.noProfile', want || '?'))
    return route
  }
  return {
    async request(method, params) {
      const r = await resolve()
      return r ? host.requestProfile(r, method, params, undefined, { spawnPriority: 'foreground' }) : host.request(method, params)
    },
    reset() {
      try { release?.() } catch { /* already gone */ }
      route = null; resolved = false; release = null
    }
  }
}

/**
 * Runs `talk` delegations against Hermes and reports each result to the
 * backend. Pure: every door is injected, so it is tested without the app.
 */
export function createDelegations({ request, onEvent, answer, log, setTimer, context, note = () => {}, tool = () => {},
  timing = () => {}, now = () => Date.now() }) {
  let sessionId = null // the linked session, or the one task runs in
  let chain = Promise.resolve() // runs that must share one session, one at a time
  const running = new Map() // call id -> { sid, settle }
  let pending = 0 // enqueued and not finished
  let generation = 0 // bumped on hang up: nothing queued before it may still run
  const cancelled = new Set()
  const waiters = new Map() // session id -> settle(payload)
  const ours = new Set() // every session a voice task runs in, for tool.* filtering

  onEvent('message.complete', event => {
    const settle = waiters.get(event?.session_id)
    if (settle) settle(event?.payload || {})
  })
  // What the agent is doing right now, for the page: our sessions only.
  for (const phase of ['start', 'complete']) {
    onEvent(`tool.${phase}`, event => {
      const sid = event?.session_id
      if (!sid || (sid !== sessionId && !ours.has(sid))) return
      const p = event?.payload || {}
      tool({ phase, id: String(p.tool_id || p.name || ''), name: String(p.name || ''), context: String(p.context || '') })
    })
  }

  function interrupt(sid, why) {
    log(why)
    void request('session.interrupt', { session_id: sid }).catch(() => {})
    ours.delete(sid)
    if (sessionId === sid) sessionId = null
  }

  let linked = null // stored id of the chat session the voice works on
  async function ensureSession(fresh) {
    if (sessionId && !fresh) return sessionId
    if (linked) {
      // the agent works inside the operator's session: same history, same chat
      const resumed = await request('session.resume', { session_id: linked, omit_messages: true })
      sessionId = resumed?.session_id || null
      if (!sessionId) throw new Error('session.resume returned no session id')
      ours.add(sessionId)
      log(T('log.linkedSession', linked))
      return sessionId
    }
    const created = await request('session.create', { title: SESSION_TITLE })
    sessionId = created?.session_id || null
    if (!sessionId) throw new Error('session.create returned no session id')
    ours.add(sessionId)
    log(T('log.voiceSession', sessionId))
    return sessionId
  }

  /** A session of its own: two tasks never share one, they would interleave. */
  async function ownSession() {
    const created = await request('session.create', { title: SESSION_TITLE })
    const sid = created?.session_id || null
    if (!sid) throw new Error('session.create returned no session id')
    ours.add(sid)
    return sid
  }

  async function submit(text, sid_of) {
    const params = sid => ({ session_id: sid, text, surface: 'voice-live', voice_context: context() })
    const sid = sid_of || await ensureSession(false)
    try {
      await request('prompt.submit', params(sid))
      return sid
    } catch (first) {
      // the session went away under us: a new one, never the same id twice
      log(T('log.refused', first?.message || first))
      const next = sid_of ? await ownSession() : await ensureSession(true)
      await request('prompt.submit', params(next))
      return next
    }
  }

  function waitFor(sid, id) {
    return new Promise(resolve => {
      let done = false
      const settle = value => {
        if (done) return
        done = true
        waiters.delete(sid)
        if (running.get(id)?.sid === sid) running.delete(id)
        resolve(value)
      }
      waiters.set(sid, settle)
      running.set(id, { sid, settle })
      setTimer(() => settle({ status: 'timeout' }), ANSWER_TIMEOUT_MS)
    })
  }

  async function run(d, gen) {
    const stale = () => gen !== generation || cancelled.has(d.id)
    if (stale()) return
    const t0 = now()
    const took = () => now() - t0
    try {
      // a linked session runs one turn at a time; elsewhere each task gets its own
      const own = linked ? null : await ownSession()
      if (stale()) { if (own) interrupt(own, T('log.cancelled')); return }
      const sid = await submit(d.prompt, own || undefined)
      if (stale()) { interrupt(sid, T('log.cancelled')); return } // cancelled during the submit
      const payload = await waitFor(sid, d.id)
      if (payload === null || stale()) return
      if (payload.status === 'timeout') {
        interrupt(sid, T('log.timeout'))
        timing({ id: d.id, request: d.request, ms: took(), ok: false })
        note(T('note.timeout', formatMs(took())))
        await answer({ id: d.id, error: "l'agent n'a pas répondu à temps" })
      } else if (payload.status === 'error' || payload.status === 'interrupted') {
        timing({ id: d.id, request: d.request, ms: took(), ok: false })
        note(T('note.failed', String(payload.text || payload.status).slice(0, 160)))
        await answer({ id: d.id, error: String(payload.text || payload.status) })
      } else {
        timing({ id: d.id, request: d.request, ms: took(), ok: true })
        note(T('note.answered', formatMs(took()), String(payload.text || '').slice(0, 200)))
        await answer({ id: d.id, answer: String(payload.text || '') })
      }
    } catch (e) {
      log(T('log.delegationFailed', e?.message || e))
      note(T('note.failed', String(e?.message || e).slice(0, 160)))
      await answer({ id: d.id, error: String(e?.message || e) }).catch(() => {})
    }
  }

  return {
    /** Work inside this stored session (null: a voice session of our own). */
    link(stored, runtime = null) {
      linked = stored || null
      sessionId = runtime || null
    },
    enqueue(d) {
      if (!d?.id) return
      if (pending >= MAX_PENDING) {
        log(T('log.tooMany'))
        void answer({ id: d.id, error: "trop de demandes en attente, attends la réponse de l'agent" }).catch(() => {})
        return
      }
      log(T('log.delegation', String(d.request || '').slice(0, 60)))
      note(T('note.looking', String(d.request || d.prompt || '').slice(0, 160)))
      pending++
      const gen = generation
      const done = () => { pending-- }
      // A linked session is the operator's own conversation: one turn at a time.
      // Everywhere else the tasks run side by side, each in its own session.
      if (linked) chain = chain.then(() => run(d, gen)).finally(done)
      else void run(d, gen).finally(done)
    },
    cancel(id) {
      cancelled.add(id)
      const hit = running.get(id)
      if (hit) {
        interrupt(hit.sid, T('log.cancelled'))
        hit.settle(null)
      }
    },
    get busy() { return pending > 0 },
    /** Hang up: the running agent turn stops, nothing queued runs. */
    reset() {
      generation++
      cancelled.clear()
      for (const { sid, settle } of [...running.values()]) {
        interrupt(sid, T('log.hungUp'))
        settle(null)
      }
    }
  }
}

// -- event feed: one poll at a time, each event handled once ------------------

/**
 * Drains the backend feed. A poll can take longer than the interval over a
 * remote backend; overlapping polls would fetch the same events and play the
 * same audio twice. So: never two polls in flight, and never an event whose
 * seq was already handled.
 */
export function createPoller({ fetch, handle, onSummary }) {
  let busy = false
  let after = 0
  return {
    async tick() {
      if (busy) return
      busy = true
      try {
        let p
        try {
          p = await fetch(after)
        } catch {
          return // transient, the next tick retries
        }
        for (const event of p?.events || []) {
          if (typeof event?.seq !== 'number' || event.seq <= after) continue
          after = event.seq
          handle(event)
        }
        if (typeof p?.next === 'number' && p.next > after) after = p.next
        onSummary(p)
      } finally {
        busy = false
      }
    },
    reset() { after = 0 }
  }
}

/** How far ahead of the speaker's clock a chunk is scheduled after the queue
 *  ran dry. A fixed 120 ms lead is smaller than one poll round trip from a
 *  desktop to a remote backend (measured: ~170 ms over a VPN), so every late poll used
 *  to cut the voice. Follow the measured latency instead, bounded. */
export function playLead(rtt) {
  const measured = Number.isFinite(rtt) && rtt > 0 ? rtt : 0.17
  return Math.min(1.2, Math.max(PLAY_LEAD_S, 2 * measured + 0.12))
}

/** Where the next chunk starts. Back to back while the queue holds; once it
 *  ran dry, `lead` ahead, and `gapMs` says how long the silence was
 *  (null on the first chunk of an answer, `head` 0). */
export function planChunk(head, now, lead = PLAY_LEAD_S) {
  if (head >= now + 0.005) return { at: head, gapMs: null }
  return { at: now + lead, gapMs: head > 0 ? Math.round((now - head) * 1000) : null }
}

// -- the operator's dictionary, same rule as the backend (prefs.apply_words) --

export function fixWords(text, words) {
  if (!text || !words?.length) return text
  const pairs = []
  for (const w of words) for (const h of w.heard || []) if (h) pairs.push([h, w.word])
  pairs.sort((a, b) => b[0].length - a[0].length)
  for (const [heard, right] of pairs) {
    const escaped = heard.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    // A function, not a string: "$&" or "$1" in a word stays literal text.
    text = text.replace(new RegExp(`(?<![\\p{L}\\p{N}_])${escaped}(?![\\p{L}\\p{N}_])`, 'giu'), () => right)
  }
  return text
}

const TOOL_LABELS = [
  [/^terminal|^process/, 'Terminal', 'terminal'], [/^read_file/, 'tool.files', 'files'],
  [/^search_files/, 'tool.search', 'search'], [/^patch|^write_file/, 'tool.write', 'write'],
  [/^web_search|^web_extract|^x_search|^mcp__crawl4ai/, 'Web', 'web'], [/^browser/, 'tool.browser', 'browser'],
  [/^execute_code/, 'Code', 'code'], [/^delegate_task/, 'tool.agents', 'agents'],
  [/^memory|^session_search|^mcp__agentmemory/, 'tool.memory', 'memory'], [/^skill/, 'tool.skill', 'skill'],
  [/^cronjob|^kanban/, 'tool.clock', 'clock'], [/^vision|^image/, 'Image', 'image'],
  [/^mcp__todoist/, 'Todoist', 'tasks']
]

/** What the step is called on the page. */
export function toolLabel(name) {
  for (const [re, label] of TOOL_LABELS) if (re.test(name || '')) return label.startsWith('tool.') ? T(label) : label
  return String(name || T('tool.tool')).replace(/^mcp__[^_]+__/, '').replace(/_/g, ' ')
}

/** Which moving icon the step gets. */
export function toolKind(name) {
  for (const [re, , kind] of TOOL_LABELS) if (re.test(name || '')) return kind
  return /^mcp__/.test(name || '') ? 'plug' : 'gear'
}

/** Agent steps shown under the orb: one entry per tool, newest last, a few kept. */
export function trackSteps(steps, ev, now = Date.now()) {
  if (ev.phase === 'start') {
    return [...steps.filter(s => s.id !== ev.id), { id: ev.id, label: toolLabel(ev.name), kind: toolKind(ev.name), context: ev.context, done: false, at: now }].slice(-6)
  }
  return steps.map(s => s.id === ev.id && !s.done ? { ...s, done: true, ms: now - s.at, slow: now - s.at > SLOW_STEP_MS } : s)
}

// -- audio helpers -----------------------------------------------------------

export function floatToPcm16(input) {
  const pcm = new Int16Array(input.length)
  let peak = 0
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]))
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff
    peak = Math.max(peak, Math.abs(s))
  }
  return { pcm, peak }
}

export function pcm16ToBase64(pcm) {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength)
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000))
  return btoa(bin)
}

export function base64ToFloat(b64) {
  const bin = atob(b64)
  const out = new Float32Array(bin.length >> 1)
  for (let i = 0; i < out.length; i++) {
    const v = (bin.charCodeAt(2 * i) & 0xff) | ((bin.charCodeAt(2 * i + 1) & 0xff) << 8)
    out[i] = (v >= 0x8000 ? v - 0x10000 : v) / 32768
  }
  return out
}

// -- resuming a call --------------------------------------------------------

/** What "Reprendre" keeps of a call: its last spoken lines. Null when silent. */
export function lastCallOf(rows, agentName, at) {
  const kept = (rows || []).filter(r => r.who === 'toi' || r.who === 'hermes').slice(-RESUME_LINES)
    .map(r => ({ who: r.who, text: String(r.text || '').slice(0, 400) })).filter(r => r.text.trim())
  return kept.length ? { at, agent: agentName || '', lines: kept } : null
}

export function readLastCall(raw) {
  try {
    const v = JSON.parse(raw || 'null')
    return Array.isArray(v?.lines) && v.lines.length ? v : null
  } catch { return null }
}

// -- the call ------------------------------------------------------------------

function createCall(ctx) {
  const status = atom('idle') // idle | connecting | live | error
  const message = atom('')
  const lines = atom([]) // { who: 'toi' | 'hermes', text }
  const speaking = atom(false)
  const thinking = atom(false)
  const hasKey = atom(true)
  //: Label of the voice service in use (Gemini Live, OpenAI Realtime, Grok Voice).
  const service = atom('Gemini Live')
  const agent = atom('') // the installed agent's name, from its SOUL.md
  const agentProfile = atom('') // the profile it is installed on: delegations run there
  const perf = atom([]) // last delegations: { request, ms, ok, steps }
  const logRows = atom([])
  const prefs = atom({ address: 'tu', words: [] })
  const steps = atom([]) // what the agent is doing, see trackSteps
  const pulse = { at: 0 } // last agent step, the orb flashes on it

  let stopPoll = null
  let mic = null // { stream, ctx, node }
  let play = null // { ctx, head, sources }
  let uploads = 0
  let owner = '' // from /start; the backend serves the feed to its owner only
  let bargeHits = 0

  const log = msg => logRows.set([...logRows.get().slice(-30), `${new Date().toLocaleTimeString()}  ${msg}`])
  const say = (who, text) => {
    const rows = lines.get()
    const last = rows[rows.length - 1]
    if (last && last.who === who && !last.closed) {
      lines.set([...rows.slice(0, -1), { ...last, text: last.text + text }])
    } else {
      lines.set([...rows.slice(-40), { who, text }])
    }
  }
  const closeLine = () => {
    const rows = lines.get()
    if (rows.length) lines.set([...rows.slice(0, -1), { ...rows[rows.length - 1], closed: true }])
  }
  const note = text => { closeLine(); lines.set([...lines.get().slice(-40), { who: 'agent', text, closed: true }]) }
  // "Effacer" hides what is on screen; the agent and a later resume still see it.
  const clearChat = () => lines.set(lines.get().map(r => ({ ...r, hidden: true, closed: true })))
  const chatShown = atom(ctx.storage?.get('chat', 'shown') !== 'hidden')
  const toggleChat = () => { chatShown.set(!chatShown.get()); ctx.storage?.set('chat', chatShown.get() ? 'shown' : 'hidden') }
  // the last call, kept on this machine only, for "Reprendre"
  const lastCall = atom(readLastCall(ctx.storage?.get('last', '')))
  const keepLast = () => {
    const v = lastCallOf(lines.get(), agent.get(), Date.now())
    if (!v) return
    lastCall.set(v); ctx.storage?.set('last', JSON.stringify(v))
  }
  const forgetLast = () => { lastCall.set(null); ctx.storage?.set('last', '') }
  // the chat session the voice works on: { stored, title }
  const linkedSession = atom(null)
  const context = () => lines.get().filter(r => r.who !== 'agent').slice(-6)
    .map(r => `${r.who === 'toi' ? 'Utilisateur' : 'Assistant'} : ${fixWords(r.text, prefs.get().words)}`).join('\n')

  const agentRpc = createProfileRequest({ host, profile: () => agentProfile.get(), log })
  const delegations = createDelegations({
    request: (method, params) => agentRpc.request(method, params),
    timing: t => perf.set([...perf.get().slice(-9), { ...t, steps: steps.get().filter(s => s.ms != null) }]),
    onEvent: (type, fn) => ctx.onEvent(type, fn),
    answer: body => ctx.rest('/answer', { method: 'POST', body: { ...body, owner } }),
    log,
    setTimer: (fn, ms) => ctx.setTimeout(fn, ms),
    context,
    note,
    tool: ev => { steps.set(trackSteps(steps.get(), ev)); if (ev.phase === 'start') pulse.at = performance.now() }
  })

  function playChunk(b64) {
    if (!play) return
    const samples = base64ToFloat(b64)
    if (!samples.length) return
    const buffer = play.ctx.createBuffer(1, samples.length, OUT_RATE)
    buffer.getChannelData(0).set(samples)
    const src = play.ctx.createBufferSource()
    src.buffer = buffer
    src.connect(play.analyser)
    const { at, gapMs } = planChunk(play.head, play.ctx.currentTime, playLead(net.rtt))
    if (gapMs != null) play.gaps.push(gapMs)
    play.head = at
    play.sources.add(src)
    src.onended = () => {
      play?.sources.delete(src)
      if (play && play.sources.size === 0) { speaking.set(false); audioReport() }
    }
    src.start(play.head)
    play.head += buffer.duration
    speaking.set(true)
  }

  function flushPlayback() {
    if (!play) return
    for (const src of play.sources) {
      try { src.onended = null; src.stop() } catch { /* already stopped */ }
    }
    play.sources.clear()
    play.head = 0
    speaking.set(false)
    audioReport()
  }

  /** After each answer: did the output skip? `playbackStats` counts what the
   *  speaker really missed; `gaps` counts a queue that ran dry. */
  function audioReport() {
    if (!play) return
    const st = play.ctx.playbackStats
    const events = st ? st.underrunEvents - play.under.events : 0
    const ms = st ? Math.round(st.underrunDuration * 1000 - play.under.ms) : 0
    if (st) play.under = { events: st.underrunEvents, ms: Math.round(st.underrunDuration * 1000) }
    const gaps = play.gaps.splice(0)
    if (events > 0) log(T('log.underrun', events, ms))
    if (gaps.length) log(T('log.dry', gaps.length, gaps.join(', ')))
    if (events > 0 || gaps.length) audioGlitches.set(audioGlitches.get() + events + gaps.length)
  }

  function playing() {
    return !!play && play.head > play.ctx.currentTime + 0.05
  }

  // A minimised window throttles timers, never the audio callback: it keeps
  // the feed polled and the floating window drawn while the app is hidden.
  let lastAudioPoll = 0
  const background = new Set()
  function audioTick() {
    const now = performance.now()
    if (document.visibilityState === 'hidden' && now - lastAudioPoll > 200) { lastAudioPoll = now; void poller.tick() }
    for (const fn of background) fn()
  }
  const muted = atom(false)
  const audioGlitches = atom(0)
  /** Muted for real: the device is released (the OS light goes off), the
   *  backend drops any chunk still in flight and tells Gemini the turn ended.
   *  Reopening asks the OS for the microphone again. */
  async function toggleMute() {
    if (muting) return
    muting = true
    const next = !muted.get()
    try {
      muted.set(next)
      if (next) {
        stopMic()
        await ctx.rest('/mute', { method: 'POST', body: { muted: true, owner } }).catch(() => {})
        log(T('log.muted'))
      } else {
        await ctx.rest('/mute', { method: 'POST', body: { muted: false, owner } }).catch(() => {})
        if (status.get() === 'live') await startMic()
        log(T('log.unmuted'))
      }
      try { navigator.mediaSession?.setMicrophoneActive?.(!next) } catch { /* not offered */ }
    } catch (e) {
      log(T('log.micUnavailable', e?.message || e))
      message.set(e?.name === 'NotAllowedError' ? T('err.micDenied') : String(e?.message || e))
    } finally {
      muting = false
    }
  }
  let muting = false

  async function startMic() {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    })
    const audioCtx = new AudioContext({ sampleRate: IN_RATE })
    const source = audioCtx.createMediaStreamSource(stream)
    const analyser = audioCtx.createAnalyser()
    analyser.fftSize = 512
    source.connect(analyser)
    const node = audioCtx.createScriptProcessor(4096, 1, 1)
    node.onaudioprocess = event => {
      if (status.get() !== 'live') return
      if (muted.get()) return
      const { pcm, peak } = floatToPcm16(event.inputBuffer.getChannelData(0))
      // Gemini only flags `interrupted` while it is still generating; a long
      // answer is already queued here, so cut it locally when the user talks.
      if (playing() && peak > BARGE_PEAK) {
        if (++bargeHits >= BARGE_BLOCKS) { bargeHits = 0; flushPlayback(); log(T('log.barge')) }
      } else bargeHits = 0
      if (uploads >= MAX_AUDIO_BACKLOG) return // behind: drop rather than lag
      uploads++
      ctx.rest('/audio', { method: 'POST', body: { data: pcm16ToBase64(pcm), owner }, timeoutMs: 4000 })
        .catch(() => {})
        .finally(() => { uploads-- })
    }
    source.connect(node)
    node.connect(audioCtx.destination)
    mic = { stream, ctx: audioCtx, node, analyser }
  }

  function stopMic() {
    if (!mic) return
    try { mic.node.onaudioprocess = null; mic.node.disconnect() } catch { /* gone */ }
    try { mic.stream.getTracks().forEach(t => t.stop()) } catch { /* gone */ }
    try { void mic.ctx.close() } catch { /* gone */ }
    mic = null
  }

  function handle(event) {
    switch (event.kind) {
      case 'audio': playChunk(event.data); break
      case 'in': say('toi', event.text); break
      case 'out': say('hermes', event.text); break
      case 'turn': closeLine(); break
      case 'interrupted': flushPlayback(); log(T('log.interrupted')); break
      case 'delegate': thinking.set(true); steps.set([]); pulse.at = performance.now(); delegations.enqueue(event); break
      case 'cancel': delegations.cancel(event.id); break
      case 'context_lost': log(T('log.contextLost')); break
      case 'closed': log(T('log.closed', event.reason)); break
    }
  }

  // Measured poll round trip: the playback buffer follows it, not a constant.
  const net = { rtt: 0.17 }
  const poller = createPoller({
    fetch: async after => {
      const t0 = performance.now()
      try {
        return await ctx.rest(`/poll?after=${after}&owner=${encodeURIComponent(owner)}`)
      } finally {
        net.rtt = net.rtt * 0.75 + (performance.now() - t0) / 1000 * 0.25
      }
    },
    handle,
    onSummary: p => {
      if (p.owner_ok === false && status.get() === 'live') {
        teardown()
        status.set('idle')
        message.set(T('err.takenOver'))
        return
      }
      hasKey.set(!!p.has_key)
      if (p.service) service.set(String(p.service))
      if (p.agent) agent.set(String(p.agent))
      if (p.profile) agentProfile.set(String(p.profile))
      thinking.set(delegations.busy)
      if (p.status !== 'live' && status.get() === 'live') {
        teardown()
        status.set(p.status === 'error' ? 'error' : 'idle')
        message.set(p.error || T('err.ended'))
      }
    }
  })

  function teardown() {
    if (stopPoll) { stopPoll(); stopPoll = null }
    stopMic()
    flushPlayback()
    if (play) {
      try { play.clock.onaudioprocess = null; play.clock.disconnect() } catch { /* gone */ }
      try { void play.ctx.close() } catch { /* gone */ }
      play = null
    }
    muted.set(false)
    delegations.reset()
    agentRpc.reset()
    thinking.set(false)
  }

  /** `resume`: carry on the previous call. A linked session is read here. */
  async function start({ resume = false } = {}) {
    if (status.get() === 'connecting') return
    if (status.get() === 'live') return stop()
    status.set('connecting')
    message.set('')
    const previous = resume ? lastCall.get() : null
    lines.set(previous ? previous.lines.map(r => ({ ...r, closed: true })) : [])
    poller.reset()
    let link = null
    const target = linkedSession.get()
    delegations.link(null)
    if (target) {
      try {
        const r = await agentRpc.request('session.resume', { session_id: target.stored })
        delegations.link(target.stored, r?.session_id)
        const rows = (r?.messages || []).filter(m => m.role === 'user' || m.role === 'assistant').slice(-RESUME_LINES)
          .map(m => ({ role: m.role, text: String(m.text || '').slice(0, 400) }))
        link = { title: target.title, lines: rows }
        log(T('log.linked', target.title, rows.length))
      } catch (e) {
        log(T('log.linkFailed', target.title, e?.message || e))
      }
    }
    try {
      // 'playback' asks the OS for a larger render buffer: no crackle when
      // the page is busy drawing; the extra latency is a few ms.
      const outCtx = new AudioContext({ sampleRate: OUT_RATE, latencyHint: 'playback' })
      const analyser = outCtx.createAnalyser()
      analyser.fftSize = 512
      analyser.connect(outCtx.destination)
      play = { ctx: outCtx, head: 0, sources: new Set(), analyser, gaps: [], under: { events: 0, ms: 0 } }
      // The background clock lives on the speaker, which runs for the whole
      // call, muted or not: a hidden window drops timers to 1/s and rAF to 0,
      // an audio callback keeps its pace (Electron 44 probe).
      const clock = outCtx.createScriptProcessor(1024, 1, 1)
      clock.onaudioprocess = event => { event.outputBuffer.getChannelData(0).fill(0); if (status.get() === 'live') audioTick() }
      clock.connect(outCtx.destination)
      play.clock = clock
      await startMic()
      log(T('log.connecting', service.get()))
      const r = await ctx.rest('/start', { method: 'POST', timeoutMs: 30000,
        body: { lang: T('meta.locale'), ...(previous ? { resume: previous.lines } : {}), ...(link ? { link } : {}) } })
      if (previous) log(T('log.resumed', previous.lines.length))
      owner = String(r?.owner || '')
      hasKey.set(!!r?.has_key)
      if (r?.service) service.set(String(r.service))
      if (r?.agent) agent.set(String(r.agent))
      if (r?.profile) agentProfile.set(String(r.profile))
      if (r?.status !== 'live') {
        teardown()
        status.set('error')
        message.set(r?.error || T('err.startFailed'))
        log(T('log.startRefused', r?.error || r?.status))
        return
      }
      status.set('live')
      log(T('log.online', r.model))
      stopPoll = ctx.setInterval(() => { void poller.tick() }, POLL_MS)
    } catch (e) {
      teardown()
      status.set('error')
      const denied = e?.name === 'NotAllowedError'
      message.set(denied ? T('err.micDenied') : String(e?.message || e))
      log(T('log.error', e?.message || e))
    }
  }

  function stop() {
    keepLast()
    teardown()
    status.set('idle')
    void ctx.rest('/stop', { method: 'POST', body: { owner } }).catch(() => {})
    owner = ''
  }

  // 0..1, the voice that is loud right now: hers while she speaks, yours otherwise.
  const buf = new Float32Array(512)
  function level() {
    // muted: the orb never moves with your voice, nothing of it is read
    const a = speaking.get() ? play?.analyser : muted.get() ? null : mic?.analyser
    if (!a) return 0
    a.getFloatTimeDomainData(buf)
    let sum = 0
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
    return Math.min(1, Math.sqrt(sum / buf.length) * 4)
  }

  /** Recent chat sessions of the agent's profile, for "Lier à une session". */
  async function listSessions() {
    const r = await agentRpc.request('session.list', { limit: 30 })
    return (r?.sessions || []).filter(x => x?.id && x.title !== SESSION_TITLE)
      .map(x => ({ stored: String(x.id), title: String(x.title || x.preview || x.id).slice(0, 80), at: x.started_at || 0 }))
  }
  function linkSession(target) {
    linkedSession.set(target || null)
    log(target ? T('log.linkSet', target.title) : T('log.linkCleared'))
  }

  async function refresh() {
    try {
      const s = await ctx.rest('/status')
      hasKey.set(!!s?.has_key)
      if (s?.service) service.set(String(s.service))
      if (s?.agent) agent.set(String(s.agent))
      if (s?.profile) agentProfile.set(String(s.profile))
    } catch { /* backend off: shown as an error on start */ }
    try {
      const p = await ctx.rest('/prefs')
      if (p && Array.isArray(p.words)) prefs.set(p)
    } catch { /* older backend: defaults */ }
    try {
      const v = await ctx.rest('/voices')
      if (v && Array.isArray(v.voices)) voices.set(v)
    } catch { /* older backend: no voice choice */ }
  }

  /** Saved on the backend, used from the next call. Throws the backend's reason. */
  async function savePrefs(next) {
    const saved = await ctx.rest('/prefs', { method: 'POST', body: next })
    if (saved?.error) throw new Error(saved.error)
    prefs.set(saved)
    if (saved.voice) voices.set({ ...voices.get(), current: saved.voice })
    return saved
  }

  const voices = atom({ service: '', voices: [], current: '' })
  const shape = atom(ctx.storage?.get('shape', 'circle') || 'circle')
  const setShape = value => { shape.set(value); ctx.storage?.set('shape', value) }

  const api = { status, message, lines, speaking, thinking, hasKey, service, agent, agentProfile, perf, logRows, prefs, voices, steps, pulse, shape, setShape,
    muted, toggleMute, audioGlitches, clearChat, chatShown, toggleChat, lastCall, forgetLast,
    linkedSession, listSessions, linkSession, start, stop, refresh, savePrefs, level,
    setTimer: (fn, ms) => ctx.setInterval(fn, ms) }
  api.floating = createFloating(api, { onTickBackground: fn => background.add(fn) })
  ctx.onDispose(() => { if (status.get() !== 'idle') stop(); api.floating.close() })
  return api
}

// -- UI ----------------------------------------------------------------------

const SHAPES = ['circle', 'square', 'oval', 'triangle']

function useCallState(call) {
  const status = useValue(call.status)
  const speaking = useValue(call.speaking)
  const thinking = useValue(call.thinking)
  const active = status === 'live' || status === 'connecting'
  const state = status === 'error' ? 'error'
    : status === 'connecting' ? 'connecting'
      : !active ? 'idle'
        : thinking ? 'thinking' : speaking ? 'speaking' : 'listening'
  const t = useT()
  return { status, active, state, label: t(`state.${state}`) }
}

// -- the orb: a shape made of small squares that comes alive -----------------

export const GRID = 30

/** 0 at the centre of the shape, 1 on its edge, above 1 outside. */
export function shapeDistance(shape, u, v) {
  if (shape === 'square') return Math.max(Math.abs(u), Math.abs(v)) / 0.86
  if (shape === 'oval') return Math.hypot(u / 0.98, v / 0.64)
  if (shape === 'triangle') {
    // equilateral, pointing up, centred on its incentre
    const cy = 0.18, r = 0.56, dy = v - cy
    return Math.max(dy, -0.5 * dy - 0.866 * u, -0.5 * dy + 0.866 * u) / r
  }
  return Math.hypot(u, v) / 0.96
}

const hash = (i, j, k = 0) => {
  const x = Math.sin(i * 12.9898 + j * 78.233 + k * 37.719) * 43758.5453
  return x - Math.floor(x)
}
const wrapAngle = a => Math.atan2(Math.sin(a), Math.cos(a))

/** Brightness 0..1 of one cell. Pure, so every state is testable. */
export function cellLevel(state, d, theta, i, j, t, lvl, burst) {
  const twinkle = Math.pow(Math.max(0, Math.sin(t * 0.9 + hash(i, j) * 6.283)), 10)
  let a
  switch (state) {
    case 'connecting':
      a = 0.12 + 0.85 * Math.exp(-Math.pow(wrapAngle(theta - t * 4.5), 2) * 5) * (0.4 + 0.6 * d)
      break
    case 'listening':
      a = 0.16 + 0.18 * twinkle + lvl * (0.35 + 0.65 * (0.5 + 0.5 * Math.sin(d * 11 - t * 7))) * (1.1 - d * 0.5)
      break
    case 'speaking':
      a = 0.2 + lvl * (0.5 + 0.5 * Math.sin(d * 15 - t * 11)) * 1.25 + lvl * (1 - d) * 0.6
      break
    case 'thinking': {
      const beam = Math.exp(-Math.pow(wrapAngle(theta - t * 2.6), 2) * 2.5) * (0.3 + 0.7 * d)
      const flip = hash(i, j, Math.floor(t * 9)) > 0.93 ? 0.7 : 0
      a = 0.14 + 0.7 * beam + flip
      break
    }
    case 'error':
      a = 0.22 + (hash(j, 0, Math.floor(t * 3)) > 0.9 ? 0.4 : 0)
      break
    default:
      a = 0.1 + 0.05 * Math.sin(t * 0.8) + 0.3 * twinkle
  }
  if (burst >= 0 && burst < 1.2) a += 0.9 * Math.exp(-Math.pow((d - burst * 1.1) / 0.12, 2))
  return Math.max(0, Math.min(1, a))
}

/**
 * Relief: the shape is a pillow, height 1 at its centre and 0 on its edge.
 * Returns the height and its slope (finite differences of the distance field).
 */
export function heightField(shape, u, v) {
  const h = (x, y) => { const d = shapeDistance(shape, x, y); return d >= 1 ? 0 : Math.sqrt(1 - d * d) }
  const e = 0.02
  return { h: h(u, v), gx: (h(u + e, v) - h(u - e, v)) / (2 * e), gy: (h(u, v + e) - h(u, v - e)) / (2 * e) }
}

/** Light on one cell: `shade` 0..1 (diffuse), `spec` 0..1 (shine). Light comes from the top left. */
export function cellRelief(gx, gy, bulge, lx = -0.5, ly = -0.62, lz = 0.6) {
  let nx = -gx * bulge * 0.55, ny = -gy * bulge * 0.55, nz = 1
  const nl = Math.hypot(nx, ny, nz); nx /= nl; ny /= nl; nz /= nl
  const ll = Math.hypot(lx, ly, lz), Lx = lx / ll, Ly = ly / ll, Lz = lz / ll
  const diffuse = Math.max(0, nx * Lx + ny * Ly + nz * Lz)
  let hx = Lx, hy = Ly, hz = Lz + 1
  const hl = Math.hypot(hx, hy, hz); hx /= hl; hy /= hl; hz /= hl
  const spec = Math.pow(Math.max(0, nx * hx + ny * hy + nz * hz), 60)
  return { shade: Math.min(1, 0.12 + 0.95 * diffuse), spec, nx, ny }
}

/**
 * Orthographic view of a grid drawn on a sphere, along each ray of the shape:
 * position r -> sin(r * pi / 2), so rows bend; a tile shrinks radially by
 * cos(r * pi / 2) and tangentially by sin(r * pi / 2) / r. `fx`, `fy` are its
 * width and height factors on screen.
 */
export function sphereProject(u, v, d) {
  if (d < 1e-6) return { px: u, py: v, fx: 1.2, fy: 1.2 }
  const q = Math.min(1, d) * Math.PI / 2
  const ft = Math.sin(q) / Math.min(1, d), fr = Math.max(0.22, Math.PI / 2 * Math.cos(q))
  const c = u / Math.hypot(u, v), sn = v / Math.hypot(u, v)
  return { px: u * ft, py: v * ft, fx: Math.hypot(fr * c, ft * sn) / Math.SQRT2 * 1.2, fy: Math.hypot(fr * sn, ft * c) / Math.SQRT2 * 1.2 }
}

/** A canvas colour from numbers: every colour here is derived from the theme hue. */
export const paint = (r, g, b, a = 1) =>
  `color(srgb ${(r / 255).toFixed(3)} ${(g / 255).toFixed(3)} ${(b / 255).toFixed(3)} / ${Math.max(0, Math.min(1, a)).toFixed(3)})`

/** Any CSS colour (the app themes use oklch) as [r, g, b]: paint one pixel, read it back. */
let probe = null
function rgbOf(css) {
  try {
    probe = probe || document.createElement('canvas').getContext('2d', { willReadFrequently: true })
    probe.clearRect(0, 0, 1, 1)
    probe.fillStyle = css
    probe.fillRect(0, 0, 1, 1)
    const [r, g, b] = probe.getImageData(0, 0, 1, 1).data
    return [r, g, b]
  } catch {
    return [120, 120, 255]
  }
}

/** The orb as a reusable drawing: the page and the floating window share it. */
export function createOrbPainter(shape) {
  // The shape fills 86 % of the box: room for its shadow and its glow.
  const SCALE = 0.86
  const cells = []
  for (let i = 0; i < GRID; i++) {
    for (let j = 0; j < GRID; j++) {
      const u = ((i + 0.5) / GRID * 2 - 1) / SCALE, v = ((j + 0.5) / GRID * 2 - 1) / SCALE
      const d = shapeDistance(shape, u, v)
      if (d > 1) continue
      // the grid wraps a sphere: spread at the centre, packed at the edge
      const { px, py, fx, fy } = sphereProject(u, v, d)
      cells.push({ i, j, d, theta: Math.atan2(v, u), px, py, fx, fy, ...heightField(shape, u, v) })
    }
  }
  return {
    cells: cells.length,
    /** Draw in the square (x0, y0, W). `level` is already smoothed. */
    draw(g, x0, y0, W, { state, level, t, burst, rgb }) {
      const [r, gr, b] = rgb
      // shadow and light are the theme hue, pushed dark or light
      const dk = [r * 0.12, gr * 0.12, b * 0.12], lt = [r + (255 - r) * 0.9, gr + (255 - gr) * 0.9, b + (255 - b) * 0.9]
      const cell = W / GRID
      g.save(); g.translate(x0, y0)
      // contact shadow, then a glow that breathes with the voice
      const sy = W * 0.925
      const shadow = g.createRadialGradient(W / 2, sy, 0, W / 2, sy, W * 0.3)
      shadow.addColorStop(0, paint(...dk, 0.42)); shadow.addColorStop(0.45, paint(...dk, 0.16)); shadow.addColorStop(1, paint(...dk, 0))
      g.save(); g.translate(0, sy); g.scale(1, 0.13); g.translate(0, -sy)
      g.fillStyle = shadow; g.fillRect(0, W * 0.5, W, W); g.restore()
      const glow = g.createRadialGradient(W / 2, W / 2, W * 0.1, W / 2, W / 2, W * 0.52)
      glow.addColorStop(0, paint(r, gr, b, 0.08 + 0.26 * level)); glow.addColorStop(1, paint(r, gr, b, 0))
      g.fillStyle = glow; g.fillRect(0, 0, W, W)
      // the light drifts a little: the relief is alive even at rest
      const lx = -0.5 + 0.18 * Math.sin(t * 0.45), ly = -0.62 + 0.12 * Math.cos(t * 0.37)
      const bulge = 1 + 1.4 * level + (state === 'thinking' ? 0.3 : 0)
      for (const c of cells) {
        const a = cellLevel(state, c.d, c.theta, c.i, c.j, t, level, burst)
        const { shade, spec } = cellRelief(c.gx, c.gy, bulge, lx, ly)
        const side = cell * SCALE * (0.42 + 0.3 * a)
        const sw = side * c.fx, sh = side * c.fy // foreshortened on the sphere
        const lift = c.h * bulge * cell * 0.1 // the front of the sphere comes forward a touch
        const cx = W / 2 + c.px * W / 2 * SCALE, cy = W / 2 + c.py * W / 2 * SCALE - lift
        const x = cx - sw / 2, y = cy - sh / 2
        const rim = 0.55 + 0.45 * Math.sqrt(c.h) // ambient occlusion toward the edge
        const k = (0.3 + 0.95 * shade) * rim // deep in the shadow, a touch past the base colour in the light
        const alpha = (0.2 + 0.8 * a) * (0.55 + 0.45 * shade)
        g.fillStyle = paint(Math.min(255, r * k), Math.min(255, gr * k), Math.min(255, b * k), alpha)
        g.fillRect(x, y, sw, sh)
        // every tile is a key: lit top-left edge, dark bottom-right edge
        const bevel = Math.max(1, Math.min(sw, sh) * 0.22)
        g.fillStyle = paint(...lt, (0.2 + 0.45 * shade) * alpha)
        g.fillRect(x, y, sw, bevel); g.fillRect(x, y + bevel, bevel, sh - bevel)
        g.fillStyle = paint(...dk, (0.15 + 0.35 * (1 - shade)) * alpha)
        g.fillRect(x + bevel, y + sh - bevel, sw - bevel, bevel); g.fillRect(x + sw - bevel, y + bevel, bevel, sh - 2 * bevel)
        if (spec > 0.03) {
          g.fillStyle = paint(...lt, Math.min(0.5, spec * 0.7 * (0.4 + 0.6 * a)))
          g.fillRect(x + bevel, y + bevel, sw - 2 * bevel, sh - 2 * bevel)
        }
      }
      g.restore()
    }
  }
}

const CALM = () => window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ? 0.3 : 1

function Orb({ call, state }) {
  const box = useRef(null)
  const canvas = useRef(null)
  useEffect(() => {
    const el = canvas.current
    if (!el?.getContext?.('2d') || !box.current) return undefined
    // The very same renderer as the detached module: one orb, two windows.
    const draw = makeOrb(el, box.current, call)
    let frame = 0
    const loop = now => { draw(now); frame = requestAnimationFrame(loop) }
    frame = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(frame)
  }, [call])
  return jsx('div', { ref: box, className: 'twh-orb', 'data-state': state, 'aria-hidden': true,
    children: jsx('canvas', { ref: canvas }) })
}

// -- the floating window: picture in picture of a drawn canvas ---------------

/** The call's state, for code outside React (the floating window). */
export function callState(status, speaking, thinking) {
  const active = status === 'live' || status === 'connecting'
  return status === 'error' ? 'error' : status === 'connecting' ? 'connecting' : !active ? 'idle'
    : thinking ? 'thinking' : speaking ? 'speaking' : 'listening'
}

/** Up to `max` lines of `text` that fit `width`; the last one ends with "…" when cut. */
export function wrapLines(measure, text, width, max) {
  const words = String(text || '').replace(/\s+/g, ' ').trim().split(' ').filter(Boolean)
  const lines = []
  let line = ''
  for (let i = 0; i < words.length; i++) {
    const next = line ? `${line} ${words[i]}` : words[i]
    if (measure(next) <= width || !line) { line = next; continue }
    lines.push(line); line = words[i]
    if (lines.length === max) { line = ''; break }
  }
  if (line && lines.length < max) lines.push(line)
  const shown = lines.join(' ').split(' ').length
  if (shown < words.length && lines.length) {
    let last = lines[lines.length - 1]
    while (last && measure(`${last}…`) > width) last = last.slice(0, -1)
    lines[lines.length - 1] = `${last.trimEnd()}…`
  }
  return lines
}

const UI_FONT = ['system-ui', '-apple-system', 'Segoe UI', 'sans-' + 'serif'].join(', ')

/** The agent's colour in its own window: the coral of the reference design. */
export const AGENT_RGB = [240, 107, 80]
const AGENT_STOPS = [[0, [246, 120, 92]], [0.55, [217, 87, 64]], [1, [178, 62, 48]]]
const CREAM = [247, 234, 224]
const PANEL = [11, 11, 13]

/** The page's orb, on any canvas, in any window. `probe` is the element that
 *  carries .twh-orb and the state, so the hue is the theme's, exactly as on
 *  the Parler page. Nothing here is a second design: same cells, same light. */
/** The theme variables the orb's hue is built from. */
const HUE_VARS = ['--ui-accent', '--accent', '--ui-success', '--ui-warning', '--ui-danger', '--ui-text-secondary']

export function makeOrb(canvas, probe, call) {
  const g = canvas.getContext('2d')
  let painter = null, shape = null, smooth = 0, rgb = [120, 120, 255], colorAt = -1
  const start = performance.now()
  const calm = CALM()
  return function draw(now = performance.now()) {
    const state = callState(call.status.get(), call.speaking.get(), call.thinking.get())
    if (shape !== call.shape.get()) { shape = call.shape.get(); painter = createOrbPainter(shape) }
    probe.dataset.state = state
    if (now - colorAt > 500) {
      // The module lives in another document, which may not carry the theme.
      // Copy the resolved hues from the app's own document, so the detached orb
      // is literally the page's colours, in dark or light, on every state.
      if (typeof document !== 'undefined' && probe.ownerDocument !== document) {
        const main = getComputedStyle(document.documentElement)
        for (const v of HUE_VARS) probe.style.setProperty(v, main.getPropertyValue(v))
      }
      const view = probe.ownerDocument?.defaultView
      rgb = rgbOf((view ? view.getComputedStyle(probe) : getComputedStyle(probe)).color)
      colorAt = now
    }
    const target = !call.muted.get() && (state === 'listening' || state === 'speaking') ? call.level() : 0
    smooth += (target - smooth) * 0.3
    const dpr = Math.min(2, canvas.ownerDocument.defaultView?.devicePixelRatio || 1)
    const css = canvas.clientWidth
    const size = css ? Math.max(1, Math.round(css * dpr)) : canvas.width // a canvas outside the DOM keeps its size
    if (canvas.width !== size) { canvas.width = size; canvas.height = size }
    g.clearRect(0, 0, size, size)
    painter.draw(g, 0, 0, size, { state, level: smooth, t: (now - start) / 1000 * calm,
      burst: call.pulse.at && calm === 1 ? (now - call.pulse.at) / 1000 : -1, rgb })
    return state
  }
}

// -- the window that stays on top ---------------------------------------------

/** The docked module: one agent, a mic switch, a hang-up. Sizes in CSS px. */
export const MODULE = { width: 76, height: 200, avatar: 72, button: 40 }

const MIC_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/></svg>'
const MIC_OFF_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M4 4l16 16"/></svg>'
const END_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 13.5c4.7-4 12.3-4 17 0l-1.9 2.6-3.6-1.4v-2.3a11 11 0 0 0-6 0v2.3l-3.6 1.4z"/></svg>'

/** Which screen edge a window sits on, from where it is. */
export function edgeOf(screenX, width, screenWidth) {
  return screenX + width / 2 < screenWidth / 2 ? 'left' : 'right'
}

function moduleCss() {
  const c = (rgb, alpha = 1) => paint(...rgb, alpha)
  return `
html,body{margin:0;background:transparent;overflow:hidden;user-select:none}
.twm{box-sizing:border-box;width:${MODULE.width}px;height:${MODULE.height}px;display:flex;flex-direction:column;align-items:center;
  gap:12px;padding:6px 0 18px;background:${c(PANEL)};border-radius:38px 10px 10px 38px;-webkit-app-region:drag;
  font:12px ${UI_FONT}}
.twm{--ui-accent:var(--accent)}
.twm[data-edge=left]{border-radius:10px 38px 38px 10px}
.twm .twh-orb{width:${MODULE.avatar}px;height:${MODULE.avatar}px;flex:none}
.twm .twh-orb canvas{width:100%;height:100%;display:block}
${ORB_CSS}
.twm button{-webkit-app-region:no-drag;width:${MODULE.button}px;height:${MODULE.button}px;flex:none;border:0;border-radius:50%;
  display:grid;place-items:center;cursor:pointer;background:${c([255, 255, 255], 0.15)};color:${c([242, 242, 246])};transition:background .15s}
.twm button:hover{background:${c([255, 255, 255], 0.16)}}
.twm button:focus-visible{outline:2px solid ${c(AGENT_RGB)};outline-offset:2px}
.twm button svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.twm button[data-on=true]{background:${c([236, 236, 240])};color:${c(PANEL)}}
.twm .end{background:${c([255, 69, 58])};color:${c([255, 255, 255])}}
.twm .end:hover{background:${c([255, 99, 89])}}
.twm .end svg{fill:currentColor;stroke:none}`
}

/** Fill a blank overlay window with the module and keep it in step with the call. */
export function mountModule(win, call, { onClose }) {
  const doc = win.document
  const style = doc.createElement('style'); style.textContent = moduleCss(); doc.head.append(style)
  const root = doc.createElement('div'); root.className = 'twm'; root.setAttribute('role', 'group')
  const probe = doc.createElement('div'); probe.className = 'twh-orb'; probe.setAttribute('aria-hidden', 'true')
  const canvas = doc.createElement('canvas'); probe.append(canvas)
  const mic = doc.createElement('button'); mic.type = 'button'; mic.className = 'mic'
  const end = doc.createElement('button'); end.type = 'button'; end.className = 'end'; end.innerHTML = END_ICON
  root.append(probe, mic, end); doc.body.append(root)
  const orb = makeOrb(canvas, probe, call)
  mic.addEventListener('click', () => { void call.toggleMute() })
  end.addEventListener('click', () => { call.stop(); onClose() })
  let shownMute = null, raf = 0, wasLive = false
  function frame() {
    if (win.closed) return onClose()
    const state = callState(call.status.get(), call.speaking.get(), call.thinking.get())
    const muted = call.muted.get()
    if (state === 'idle' && wasLive) return onClose() // the call ended: so does its window
    if (state !== 'idle') wasLive = true
    orb()
    if (shownMute !== muted) {
      shownMute = muted
      mic.innerHTML = muted ? MIC_OFF_ICON : MIC_ICON
      mic.dataset.on = String(muted)
      mic.title = mic.ariaLabel = muted ? T('btn.unmute') : T('btn.mute')
      end.title = end.ariaLabel = T('btn.hangup')
      root.ariaLabel = call.agent.get() || T('page.YourAgent')
    }
    root.dataset.edge = edgeOf(win.screenX, MODULE.width, win.screen?.availWidth || win.screen?.width || 0)
    raf = win.requestAnimationFrame(frame)
  }
  frame()
  return () => { win.cancelAnimationFrame?.(raf); if (!win.closed) win.close() }
}

/**
 * The call outside the app. Where Desktop offers it (`host.openOverlayWindow`),
 * a slim module docked to the screen edge, on top of every app, with its own
 * mic and hang-up buttons. Elsewhere (an older Desktop) the browser's video
 * picture-in-picture, fed by a canvas: drawn, not HTML, with the system's
 * mic and hang-up controls. Its clock is the speaker's audio callback while the
 * app window is hidden: timers drop to 1/s there, audio keeps its pace.
 */
function createFloating(call, { onTickBackground }) {
  const W = 360, H = 450
  let video = null, canvas = null, g = null, art = null, timer = null, unmount = null, orb = null, probe = null
  let lastDraw = 0
  const open = atom(false)
  function frame() {
    if (!g) return
    const now = performance.now()
    lastDraw = now
    orb(now)
    g.fillStyle = paint(...PANEL); g.fillRect(0, 0, W, H)
    g.drawImage(art, (W - art.width) / 2, H * 0.08)
    const name = call.agent.get() || 'Hermes'
    g.textAlign = 'center'; g.font = `600 ${Math.round(H * 0.055)}px ${UI_FONT}`
    g.fillStyle = paint(...CREAM, 0.92); g.fillText(name, W / 2, H * 0.88)
  }
  function tick() { if (g && performance.now() - lastDraw > 55) frame() }
  async function openModule() {
    const win = await host.openOverlayWindow?.({ edge: 'right', width: MODULE.width, height: MODULE.height }).catch(() => null)
    if (!win) return false
    unmount = mountModule(win, call, { onClose: close })
    open.set(true)
    return true
  }
  async function openPip() {
    if (!document.pictureInPictureEnabled) throw new Error(T('err.noPip'))
    canvas = document.createElement('canvas'); canvas.width = W; canvas.height = H
    art = document.createElement('canvas'); art.width = art.height = Math.round(W * 0.84)
    probe = document.createElement('div'); probe.className = 'twh-orb'
    probe.style.cssText = 'position:fixed;width:1px;height:1px;opacity:0;pointer-events:none;bottom:0;left:0'
    document.body.append(probe)
    orb = makeOrb(art, probe, call)
    g = canvas.getContext('2d')
    frame()
    video = document.createElement('video')
    video.muted = true; video.playsInline = true
    video.style.cssText = 'position:fixed;width:1px;height:1px;opacity:0;pointer-events:none;bottom:0;right:0'
    video.srcObject = canvas.captureStream(30)
    document.body.append(video)
    timer = call.setTimer(tick, 33)
    await video.play()
    video.addEventListener('leavepictureinpicture', close, { once: true })
    await video.requestPictureInPicture()
    open.set(true)
    try {
      navigator.mediaSession?.setActionHandler?.('hangup', () => call.stop())
      navigator.mediaSession?.setActionHandler?.('togglemicrophone', () => call.toggleMute())
    } catch { /* not offered by this browser */ }
  }
  async function detach() {
    if (open.get()) return close()
    if (await openModule()) return
    await openPip()
  }
  function close() {
    open.set(false)
    if (unmount) { const u = unmount; unmount = null; u() }
    if (video) { void document.exitPictureInPicture?.().catch(() => {}) }
    if (timer) { timer(); timer = null }
    try { video?.srcObject?.getTracks?.().forEach(tr => tr.stop()) } catch { /* gone */ }
    video?.remove(); probe?.remove(); video = null; canvas = null; g = null; art = null; orb = null; probe = null
    try {
      navigator.mediaSession?.setActionHandler?.('hangup', null)
      navigator.mediaSession?.setActionHandler?.('togglemicrophone', null)
    } catch { /* not offered */ }
  }
  onTickBackground(tick)
  const available = () => typeof host.openOverlayWindow === 'function' || document.pictureInPictureEnabled === true
  return { open, detach, close, available }
}

// -- settings: shape, tu or vous, dictionary ---------------------------------

function Settings({ call, onClose }) {
  const t = useT()
  const name = useValue(call.agent) || t('page.YourAgent')
  const shape = useValue(call.shape)
  const saved = useValue(call.prefs)
  const [address, setAddress] = useState(saved.address || 'tu')
  const catalog = useValue(call.voices)
  const [voice, setVoice] = useState(saved.voice || catalog.current || '')
  const [rows, setRows] = useState(() => (saved.words || []).map(w => ({ word: w.word, heard: (w.heard || []).join(', ') })))
  const [error, setError] = useState('')
  const [done, setDone] = useState('')
  const edit = (i, key, value) => setRows(rows.map((r, k) => k === i ? { ...r, [key]: value } : r))
  const panel = useRef(null)
  useEffect(() => { panel.current?.focus?.() }, [])

  async function save() {
    setError(''); setDone('')
    try {
      await call.savePrefs({ address, voice, words: rows.map(r => ({ word: r.word, heard: r.heard.split(',') })) })
      setDone(t('settings.saved'))
    } catch (e) {
      setError(String(e?.message || e))
    }
  }

  return jsxs('div', { ref: panel, tabIndex: -1, className: 'twh-settings', role: 'dialog', 'aria-label': t('settings.aria'),
    onKeyDown: e => { if (e.key === 'Escape') { e.stopPropagation(); onClose() } }, children: [
    jsxs('div', { className: 'twh-row', children: [
      jsx('div', { className: 'twh-state', style: { flex: 1 }, children: t('settings.title') }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: onClose, children: t('settings.close') })
    ] }),
    jsxs('div', { className: 'twh-field', children: [
      jsx('div', { className: 'twh-label', children: t('settings.shape') }),
      jsx('div', { className: 'twh-hint', children: t('settings.shape.hint') }),
      jsx(SegmentedControl, { options: SHAPES.map(id => ({ id, label: t(`shape.${id}`) })), value: shape, onChange: call.setShape })
    ] }),
    jsxs('div', { className: 'twh-field', children: [
      jsx('div', { className: 'twh-label', children: t('settings.address', name) }),
      jsx(SegmentedControl, { options: [{ id: 'tu', label: t('settings.address.tu') }, { id: 'vous', label: t('settings.address.vous') }],
        value: address, onChange: setAddress })
    ] }),
    catalog.voices.length > 0 && jsxs('div', { className: 'twh-field', children: [
      jsx('div', { className: 'twh-label', children: t('settings.voice') }),
      jsx('div', { className: 'twh-hint', children: t('settings.voice.hint', catalog.voices.length, catalog.service || t('settings.service')) }),
      jsx('div', { className: 'twh-voices', role: 'radiogroup', 'aria-label': t('settings.voice'), children:
        catalog.voices.map(v => jsxs('button', { type: 'button', role: 'radio', 'aria-checked': v.id === voice,
          className: 'twh-voice', 'data-on': String(v.id === voice), onClick: () => setVoice(v.id), children: [
            jsx('b', { children: v.label }), jsx('span', { children: v.trait ? t(`trait.${v.trait}`) : v.character || '' })
          ] }, v.id)) })
    ] }),
    jsxs('div', { className: 'twh-field', children: [
      jsx('div', { className: 'twh-label', children: t('settings.dict') }),
      jsx('div', { className: 'twh-hint', children: t('settings.dict.hint') }),
      jsxs('div', { className: 'twh-words', children: [
        rows.length > 0 && jsxs('div', { className: 'twh-word twh-hint', children: [
          jsx('span', { children: t('settings.dict.word') }), jsx('span', { children: t('settings.dict.heard') }), jsx('span', {})
        ] }),
        ...rows.map((r, i) => jsxs('div', { className: 'twh-word', children: [
          jsx(Input, { value: r.word, placeholder: 'Hermes', onChange: e => edit(i, 'word', e.target.value) }),
          jsx(Input, { value: r.heard, placeholder: 'Ermes, Herms', onChange: e => edit(i, 'heard', e.target.value) }),
          jsx('button', { type: 'button', className: 'twh-x', 'aria-label': t('settings.dict.remove'), title: t('settings.dict.remove'),
            onClick: () => setRows(rows.filter((_, k) => k !== i)), children: '×' })
        ] }, i))
      ] }),
      jsx('div', { children: jsx(Button, { size: 'sm', variant: 'outline',
        onClick: () => setRows([...rows, { word: '', heard: '' }]), children: t('settings.dict.add') }) })
    ] }),
    error && jsx('div', { className: 'twh-error', role: 'alert', children: error }),
    done && jsx('div', { className: 'twh-hint', role: 'status', children: done }),
    jsxs('div', { className: 'twh-row', children: [
      jsx(Button, { size: 'sm', onClick: () => { void save() }, children: t('settings.save') }),
      jsx('span', { className: 'twh-hint', children: t('settings.footer') })
    ] })
  ] })
}

// -- the page -----------------------------------------------------------------

const svg = (children, cls = '') => jsx('svg', { viewBox: '0 0 24 24', 'aria-hidden': true, className: cls, children })
const el = (tag, props) => jsx(tag, props)
/** One small animated drawing per kind of work. Pure SVG, theme colour only. */
const ICONS = {
  web: () => svg([el('circle', { cx: 12, cy: 12, r: 8 }), el('path', { d: 'M4 12h16M12 4c3 3 3 13 0 16', className: 'twh-a-mer' }),
    el('ellipse', { cx: 12, cy: 12, rx: 3.5, ry: 8, className: 'twh-a-mer' })]),
  terminal: () => svg([el('rect', { x: 3, y: 5, width: 18, height: 14, rx: 2.5 }), el('path', { d: 'M7 10l3 2-3 2' }),
    el('path', { d: 'M12 15h5', className: 'twh-a-blink' })]),
  files: () => svg([el('path', { d: 'M7 3h7l4 4v14H7z' }), el('path', { d: 'M9.5 12h6M9.5 15h6' }),
    el('path', { d: 'M8.5 13.5h8', className: 'twh-a-scan', strokeWidth: 2.4, opacity: 0.55 })]),
  search: () => svg(el('g', { className: 'twh-a-orbit', children: [el('circle', { cx: 10.5, cy: 10.5, r: 5 }), el('path', { d: 'M14.2 14.2L19 19' })] })),
  write: () => svg([el('path', { d: 'M4 19h16', opacity: 0.5 }), el('g', { className: 'twh-a-write', children: el('path', { d: 'M14.5 5.5l3 3L9 17H6v-3z' }) })]),
  browser: () => svg([el('rect', { x: 3, y: 4, width: 18, height: 16, rx: 2.5 }), el('path', { d: 'M3 9h18' }),
    el('path', { d: 'M5 14h14', className: 'twh-a-fill', strokeWidth: 2.4 })]),
  code: () => svg(el('g', { className: 'twh-a-beat', children: [el('path', { d: 'M9 7l-5 5 5 5' }), el('path', { d: 'M15 7l5 5-5 5' })] })),
  agents: () => svg([el('circle', { cx: 12, cy: 12, r: 2.5 }), el('g', { className: 'twh-a-orbit', children: [
    el('circle', { cx: 12, cy: 4.5, r: 2 }), el('circle', { cx: 12, cy: 19.5, r: 2 })] })]),
  memory: () => svg(el('g', { className: 'twh-a-beat', children: [el('path', { d: 'M12 4l8 4-8 4-8-4z' }),
    el('path', { d: 'M4 12l8 4 8-4' }), el('path', { d: 'M4 16l8 4 8-4' })] })),
  skill: () => svg(el('g', { className: 'twh-a-beat', children: el('path', { d: 'M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.5 6.7 19.4l1.2-6-4.5-4.1 6-.7z' }) })),
  clock: () => svg([el('circle', { cx: 12, cy: 12, r: 8 }), el('path', { d: 'M12 12V7', className: 'twh-a-spin' }), el('path', { d: 'M12 12l3 2' })]),
  image: () => svg([el('rect', { x: 3, y: 5, width: 18, height: 14, rx: 2.5 }), el('path', { d: 'M3 17l5-5 4 4 3-3 6 6' }),
    el('circle', { cx: 16, cy: 9, r: 1.8, className: 'twh-a-beat' })]),
  tasks: () => svg([el('rect', { x: 4, y: 4, width: 16, height: 16, rx: 4 }), el('path', { d: 'M8 12.5l2.7 2.7L16 9.5', className: 'twh-a-tick' })]),
  plug: () => svg(el('g', { className: 'twh-a-beat', children: [el('path', { d: 'M9 3v5M15 3v5' }), el('path', { d: 'M6 8h12v3a6 6 0 0 1-12 0z' }), el('path', { d: 'M12 17v4' })] })),
  gear: () => svg(el('g', { className: 'twh-a-spin', children: [el('circle', { cx: 12, cy: 12, r: 3 }),
    el('path', { d: 'M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1' })] })),
  done: () => svg(el('path', { d: 'M6 12.5l4 4L18 8' }))
}

/** Steps on each side of the orb, newest nearest the middle of its column. */
export function sideOf(index) { return index % 2 === 0 ? 'right' : 'left' }

function Step({ s, side }) {
  const t = useT()
  const icon = (s.done ? ICONS.done : ICONS[s.kind] || ICONS.gear)()
  const when = s.done && s.ms != null ? ` · ${formatMs(s.ms)}${s.slow ? t('step.slow') : ''}` : ''
  return jsxs('div', { className: 'twh-step', 'data-done': String(!!s.done), 'data-slow': String(!!s.slow), title: s.context, children: [
    jsx('span', { className: 'twh-link', 'aria-hidden': true }),
    jsx('span', { className: 'twh-ico', children: icon }),
    jsxs('span', { className: 'twh-txt', children: [jsx('b', { children: s.label + when }), s.context && jsx('small', { children: s.context })] })
  ] })
}

function StepSide({ steps, side }) {
  const mine = steps.map((s, i) => ({ s, i })).filter(({ i }) => sideOf(i) === side)
  const t = useT()
  if (!mine.length) return null
  return jsx('div', { className: 'twh-side', 'data-side': side, 'aria-label': t('steps.aria'), children:
    mine.map(({ s }) => jsx(Step, { s, side }, s.id)) })
}

function Perf({ call }) {
  const t = useT()
  const perf = useValue(call.perf)
  const glitches = useValue(call.audioGlitches)
  if (!perf.length && !glitches) return null
  const ok = perf.filter(p => p.ok)
  const avg = ok.length ? ok.reduce((n, p) => n + p.ms, 0) / ok.length : 0
  return jsxs('details', { className: 'twh-perf', children: [
    jsx('summary', { children: t('perf.summary', perf.length, ok.length ? formatMs(avg) : '', glitches) }),
    ...perf.slice().reverse().map((p, i) => {
      const slowest = (p.steps || []).reduce((m, st) => (!m || st.ms > m.ms ? st : m), null)
      return jsxs('div', { className: 'twh-perf-row', 'data-slow': String(p.ms > SLOW_STEP_MS), children: [
        jsx('b', { children: p.ok ? formatMs(p.ms) : t('perf.failed', formatMs(p.ms)) }),
        jsx('span', { children: `${String(p.request || '').slice(0, 70)}${slowest ? t('perf.slowest', slowest.label, formatMs(slowest.ms)) : ''}` })
      ] }, i)
    })
  ] })
}

function TalkPage({ call }) {
  const t = useT()
  const { status, active, state, label } = useCallState(call)
  const message = useValue(call.message)
  const lines = useValue(call.lines)
  const hasKey = useValue(call.hasKey)
  const serviceLabel = useValue(call.service)
  const name = useValue(call.agent) || t('page.yourAgent')
  const logRows = useValue(call.logRows)
  const prefs = useValue(call.prefs)
  const steps = useValue(call.steps)
  const floating = useValue(call.floating.open)
  const muted = useValue(call.muted)
  const pipOk = typeof document !== 'undefined' && call.floating.available()
  const chatShown = useValue(call.chatShown)
  const lastCall = useValue(call.lastCall)
  const linked = useValue(call.linkedSession)
  const [open, setOpen] = useState(false)
  const [picking, setPicking] = useState(false)
  useEffect(() => { void call.refresh() }, [call])

  const sub = !hasKey && !active ? null
    : status === 'error' || (status === 'idle' && message) ? message
      : muted && active ? t('sub.muted')
      : state === 'listening' ? t(prefs.address === 'vous' ? 'sub.listen.vous' : 'sub.listen.tu')
        : state === 'thinking' ? t('sub.thinking', name)
          : !active ? t('sub.idle') : ''

  const shown = lines.filter(r => !r.hidden)
  const empty = !chatShown || shown.length === 0
  return jsxs('div', { className: 'twh-page', 'data-empty': empty || undefined, children: [
    jsxs('div', { className: 'twh-top', children: [
      pipOk && jsx(Button, { size: 'sm', variant: 'ghost', title: t('btn.detach.title'),
        onClick: () => { call.floating.detach().catch(e => call.message.set(String(e?.message || e))) },
        children: floating ? t('btn.attach') : t('btn.detach') }),
      shown.length > 0 && chatShown && jsx(Button, { size: 'sm', variant: 'ghost', title: t('btn.clear.title'),
        onClick: () => call.clearChat(), children: t('btn.clear') }),
      jsx(Button, { size: 'sm', variant: 'ghost', 'aria-pressed': !chatShown, onClick: () => call.toggleChat(),
        children: chatShown ? t('btn.hideChat') : t('btn.showChat') }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setOpen(!open), children: t('btn.settings') })
    ] }),
    open && jsx(Settings, { call, onClose: () => setOpen(false) }),
    jsxs('div', { className: 'twh-stage', children: [
      jsxs('div', { className: 'twh-orbwrap', children: [
        jsx(Orb, { call, state }),
        state === 'thinking' && jsx(StepSide, { steps, side: 'left' }),
        state === 'thinking' && jsx(StepSide, { steps, side: 'right' })
      ] }),
      jsx('div', { className: 'twh-state', role: 'status', children: active ? label : t('page.talkTo', name) }),
      !hasKey && !active
        ? jsx('div', { className: 'twh-sub', children: t('key.needed', serviceLabel) })
        : jsx('div', { className: 'twh-sub', children: sub }),
      !hasKey && !active
        ? jsx(Button, { size: 'sm', onClick: () => host.navigate(pluginSettingsHref(ID)), children: t('key.add', serviceLabel) })
        : jsxs('div', { className: 'twh-row', children: [
          !active && lastCall && jsx(Button, { size: 'sm', title: t('btn.resume.title', lastCall.lines.length, new Date(lastCall.at).toLocaleString(t('meta.locale'))),
            onClick: () => { void call.start({ resume: true }) }, children: t('btn.resume') }),
          jsx(Button, { size: 'sm', variant: active ? 'outline' : lastCall ? 'ghost' : 'default', onClick: () => { void call.start() },
            children: active ? t('btn.hangup') : status === 'error' ? t('btn.retry') : lastCall ? t('btn.newCall') : t('btn.start') }),
          status === 'live' && jsx(Button, { size: 'sm', variant: muted ? 'default' : 'ghost', 'aria-pressed': muted,
            onClick: () => call.toggleMute(), children: muted ? t('btn.unmute') : t('btn.mute') })
        ] })
    ] }),
    hasKey && jsx(SessionLink, { call, linked, active, picking, setPicking }),
    !empty && jsx('div', { className: 'twh-feed', children:
      jsx('div', { className: 'twh-feed-inner', children:
        shown.slice(-60).map((r, i) => jsx('div', { className: 'twh-msg', 'data-who': r.who,
          children: r.who === 'agent' ? t('msg.agent', fixWords(r.text, prefs.words))
            : r.who === 'toi' ? fixWords(r.text, prefs.words) : r.text }, i))
      })
    }),
    jsx(Perf, { call }),
    logRows.length > 0 && jsxs('details', { className: 'twh-details', children: [
      jsx('summary', { children: t('details') }),
      jsx('div', { className: 'twh-log', 'aria-label': t('log.aria'), children:
        logRows.map((row, i) => jsx('div', { children: row }, i))
      })
    ] })
  ] })
}

/** Which chat session the voice works on. Chosen before the call: the
 *  voice is told about it at start, the agent then works inside it. */
function SessionLink({ call, linked, active, picking, setPicking }) {
  const t = useT()
  const [list, setList] = useState(null)
  const [error, setError] = useState('')
  const open = async () => {
    setPicking(!picking)
    if (picking) return
    setError(''); setList(null)
    try { setList(await call.listSessions()) } catch (e) { setError(String(e?.message || e)) }
  }
  if (linked) {
    return jsxs('div', { className: 'twh-link-row', children: [
      jsx('span', { children: t('link.session', linked.title) }),
      !active && jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => call.linkSession(null), children: t('link.unlink') })
    ] })
  }
  if (active) return null
  return jsxs('div', { className: 'twh-link-box', children: [
    jsx(Button, { size: 'sm', variant: 'ghost', 'aria-expanded': picking, onClick: () => { void open() },
      children: picking ? t('link.close') : t('link.open') }),
    picking && jsxs('div', { className: 'twh-sessions', role: 'listbox', 'aria-label': t('link.aria'), children: [
      error && jsx('div', { className: 'twh-hint', children: t('link.unavailable', error) }),
      !error && list === null && jsx('div', { className: 'twh-hint', children: t('link.loading') }),
      list?.length === 0 && jsx('div', { className: 'twh-hint', children: t('link.none') }),
      ...(list || []).map(x => jsx('button', { type: 'button', role: 'option', className: 'twh-session',
        onClick: () => { call.linkSession(x); setPicking(false) },
        children: [jsx('b', { children: x.title }), x.at ? jsx('span', { children: new Date(x.at * 1000).toLocaleDateString(t('meta.locale')) }) : null] }, x.stored))
    ] })
  ] })
}

/** Status bar chip, only while a call runs: the call keeps going on any page. */
function CallChip({ call }) {
  const t = useT()
  const { active, state, label } = useCallState(call)
  if (!active) return null
  return jsxs('button', { type: 'button', className: 'twh-chip', title: t('chip.title'),
    onClick: () => host.navigate(ROUTE), children: [
      jsx('span', { className: 'twh-dot', 'data-state': state, 'aria-hidden': true }),
      jsx('span', { children: t('chip.label', label) })
    ] })
}

export default {
  id: ID,
  name: 'Talk with Hermes',
  description: "Parle à ton agent en direct, depuis l'onglet Parler.",
  defaultEnabled: false,
  register(ctx) {
    if (ctx.i18n?.register) {
      ctx.i18n.register(BUNDLES)
      hostT = ctx.i18n.t
      ctx.onDispose(() => { hostT = null })
    }
    const style = document.createElement('style')
    style.dataset.plugin = ID
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())
    const call = createCall(ctx)
    ctx.registerMany([{ area: STATUSBAR_AREAS.right, id: 'call', order: 5, render: () => jsx(CallChip, { call }) }])
    // Desktop applies display.language after plugins load. Same ids replace in place,
    // so the row, the tab title and the command follow each switch without a remount.
    const labels = () => ctx.registerMany([
      { area: ROUTES_AREA, data: { path: ROUTE }, id: 'page', title: T('nav.talk'), render: () => jsx(TalkPage, { call }) },
      { area: SIDEBAR_NAV_AREA, data: { codicon: 'mic', label: T('nav.talk'), path: ROUTE }, id: 'nav' },
      { area: PALETTE_AREA, id: 'open', data: { id: 'talk-with-hermes.open', label: T('palette.open'),
        keywords: ['parler', 'talk', 'voix', 'voice', 'vocal', 'appel', 'call', 'micro', 'mic', 'hablar', 'voz', 'llamada'], run: () => host.navigate(ROUTE) } }
    ])
    labels()
    ctx.i18n?.onLocaleChange?.(labels)
  }
}
