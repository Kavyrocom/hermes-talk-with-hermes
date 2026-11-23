# talk-with-hermes

Parler à un agent Hermes à la voix, en duplex : dans Hermes Desktop et dans les salons vocaux Discord.

## État

| Surface ou service | État |
|---|---|
| Hermes Desktop avec Gemini Live | Utilisé en appel réel depuis le 20/11/2026. |
| Salons vocaux Discord | Codé et testé contre des faux. En réel, seule la connexion vocale est prouvée : le bot rejoint le salon. Un appel complet (écoute, réponse, délégation à l'agent) reste à faire. |
| OpenAI Realtime, Grok Voice | Écrits depuis la documentation des fournisseurs, testés contre un faux serveur, jamais en appel réel. |

Un modèle n'est annoncé comme pris en charge qu'après un appel réel : une phrase dite, une réponse entendue.

## Installation

1. Hermes Desktop, Réglages, Plugins : installer `talk-with-hermes` dans le profil de l'agent.
2. Page de réglages du plugin : coller la clé du service vocal (Gemini par défaut). C'est le seul endroit où une clé se saisit. Desktop et les salons vocaux Discord de tous les agents la lisent là, elle n'est copiée nulle part.
3. La clé de la porte locale de chaque agent (`API_SERVER_KEY`, 127.0.0.1) n'est pas à saisir : au premier appel vocal, le plugin la crée dans le `.env` du profil de l'agent si elle manque. Il n'en remplace jamais une existante.

Dépendance Python déclarée dans le manifeste : `numpy` 2.x. La partie Discord s'appuie sur la plateforme Discord de Hermes et ses dépendances vocales (`discord.py[voice]`).

## Discord

Un salon vocal routé vers un agent est son salon vocal. Une ligne dans `gateway.profile_routes` du `config.yaml` du gateway :

```yaml
gateway:
  profile_routes:
    - name: <agent>-vocal
      platform: discord
      guild_id: '<id du serveur>'
      chat_id: '<id du salon vocal>'
      profile: <agent>
```

Le vocal applique les routes du gateway avec sa propre règle : celles qu'il a chargées au démarrage, comme pour les salons texte. Une nouvelle ligne compte donc après un redémarrage du gateway. Une ligne `enabled: false`, réservée à un `user_id`, prévue pour le bot d'un autre profil (`bot_profile`) ou pour un autre serveur n'ouvre pas d'appel : un appel sert tout le salon, avec le bot du gateway. Si deux lignes visent le même salon, la plus précise l'emporte.

Tu entres dans le salon : l'agent arrive et l'appel démarre. Tu sors : il part quelques secondes après. `/talk` depuis le salon texte d'un agent le fait venir dans ton salon vocal, `/talk leave` raccroche.

Seuls les comptes de l'allowlist Discord du gateway (`DISCORD_ALLOWED_USERS` ou rôles) sont entendus, et eux seuls peuvent utiliser `/talk` et `/talk leave`. `DISCORD_ALLOWED_CHANNELS` seul ouvre un salon texte, pas la voix.

## Prérequis réseau : UDP sortant

La voix Discord passe en UDP. Si le serveur ne reçoit pas de réponse UDP d'internet, le bot entre dans le salon mais ne parle jamais. Dans les logs : `Voice handshake complete` puis `Timed out connecting to voice` 30 s après.

Sur un VPS Hetzner Cloud, c'est le pare-feu Cloud qu'il faut ouvrir (le pare-feu de la machine peut tout autoriser, ça ne suffit pas) :

- Console Hetzner Cloud, Firewalls, le pare-feu du serveur.
- Outbound rules : protocole UDP, ports any (ou `50000-65535`), destination Any IPv4 et Any IPv6.

Vérifier depuis le serveur :

```bash
tailscale netcheck | grep UDP   # UDP: true
```

Sans Tailscale, n'importe quel client STUN (par exemple vers `stun.cloudflare.com:3478`) doit recevoir une réponse.

## Ce que le plugin fait sur ta machine

- **Audio** : pendant l'appel, ce que tu dis (micro de Desktop, ou salon vocal Discord) part en continu chez le service vocal choisi : Google, OpenAI ou xAI.
- **Identité** : le nom de l'agent, le premier paragraphe de son `SOUL.md` et sa section `## Style` partent aussi chez ce service, bornés à 40, 400 et 700 caractères. Rien d'autre du profil : ni le reste du `SOUL.md`, ni la mémoire.
- **Délégation** : quand la voix confie une demande à l'agent, elle passe par l'API server local du gateway (127.0.0.1), sous les approbations habituelles de Hermes. La demande et la réponse restent dans les sessions de l'agent, comme toute conversation Hermes.
- **Discord** : une tâche de fond, dans le gateway, surveille les salons vocaux routés vers un agent et y fait entrer l'agent quand un membre autorisé arrive.
- **Fichiers** : dans le profil où le plugin est installé, `plugin-state/talk-with-hermes/prefs.json` (tutoiement, dictionnaire) et `resume.json` (le seul jeton de reprise de session que donne Gemini). Le plugin n'écrit ni audio ni transcription sur le disque du serveur.
- **Desktop** : les 16 dernières répliques du dernier appel, 400 caractères au plus chacune, restent dans le stockage du plugin sur ton ordinateur, pour reprendre la conversation. Sans expiration pour l'instant.
- **Gemini, quota gratuit** : sur le quota gratuit de l'API Gemini, Google utilise le contenu envoyé et les réponses pour améliorer ses produits ([conditions de l'API Gemini](https://ai.google.dev/gemini-api/terms), section Unpaid Services). À savoir avant d'y faire passer des données sensibles.

## Limites connues

- N'active pas ce plugin à côté de `hermes-talk` sur le même Hermes : les deux déclarent `/talk`, et le dernier chargé gagne sans prévenir.
- Hermes n'a pas encore de surface publique pour deux besoins du plugin, importés en privé : `gateway.run._async_profile_runtime_scope` et `gateway.run._gateway_runner_ref`. La partie Discord lit aussi des internes du récepteur vocal de Hermes. Rien ne les contrôle pendant l'appel : le test de contrat (section Tests) les vérifie contre le Hermes installé. Relance-le après chaque mise à jour de Hermes.
- Si l'API server du gateway écoute sur une adresse précise autre que la boucle locale, la clé de la porte locale y circule en clair (http). Garde-le sur 127.0.0.1.
- Le texte que l'agent renvoie revient au modèle vocal. Un contenu piégé lu par l'agent (une page web, par exemple) peut pousser la voix à lui confier une nouvelle demande : tu entends l'annonce et la réponse, pas la demande elle-même. Les approbations de Hermes restent la barrière.
- Desktop : si l'envoi d'une demande à l'agent échoue, elle est relancée dans une nouvelle session, et peut alors tourner deux fois.
- OpenAI Realtime et Grok Voice : une demande que tu annules continue, et sa réponse est envoyée au modèle.

## Sécurité

Un scan de sécurité statique (hermes-security) a porté sur une version antérieure, privée (`ac8baef`) : statut `partial` (les 28 fichiers du périmètre relus, mais deux fiches de constat restées incomplètes), 25 constats, 16 faibles et 9 informatifs, aucun de sévérité moyenne ou plus. Cette version en corrige 11, les limites ci-dessus en déclarent 5, et les 9 autres, faibles ou informatifs, sont des durcissements en cours. Un nouveau scan de cette version publiée est prévu avant la soumission au catalogue de Hermes.

Pour signaler une faille : en privé, onglet Security de ce repo, « Report a vulnerability ». Pas d'issue publique.

## Tests

```bash
pytest
```

Le test de contrat avec les internes du host est ignoré quand Hermes n'est pas importable. Pour le jouer contre le Hermes installé, avec l'interpréteur qui fait tourner le gateway (`<python>` : le Python de Hermes, `<hermes>` : sa copie source, `<pytest>` : un dossier où pytest est installé pour ce Python) :

```bash
HERMES_HOME="$(mktemp -d)" TWH_HOST_CONTRACT=1 <python> -I -c "
import os, sys
sys.path.insert(0, '<hermes>'); import hermes_bootstrap
sys.path[:0] = ['<pytest>', os.getcwd()]; import pytest
sys.exit(pytest.main(['-q', 'tests']))"
```

`hermes_bootstrap` charge les dépendances de Hermes comme au démarrage du gateway. Avec `TWH_HOST_CONTRACT=1`, un Hermes introuvable fait échouer le test au lieu de l'ignorer. Le `HERMES_HOME` jetable garde la suite loin des clés du profil.

## Licence

MIT, voir [`LICENSE`](LICENSE). Code adapté et idées reprises d'autres projets : [`NOTICE.md`](NOTICE.md).
