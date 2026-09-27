import os
import sys
import re
import time
import math
import uuid
import threading
import copy
import hashlib
import secrets
import base64
import waitress  # AJOUT NOBELLIS (production, 16/09/2026) : serveur pur Python, multi-thread,
# fonctionne identiquement sur Pydroid 3 (Android)/Windows/Linux/Mac - remplace le serveur de
# développement Flask (app.run()) qui traitait les requêtes une par une (pas de threaded=True).
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from flask import Flask, render_template, request, jsonify, make_response, url_for, session
# CORRECTIF NOBELLIS (audit) : `redirect` reste hors des imports - jamais utilisé nulle part dans
# ce fichier (vérifié par recherche exhaustive). `session` est réintroduit ici (16/09/2026,
# comptes séparés Étape 1) - exactement comme annoncé dans le commentaire qu'il remplace : "le
# jour où une vraie authentification multi-utilisateur remplacera DATA_PROFIL (chantier séparé)".
# `url_for` reste nécessaire pour le filtre Jinja `logo_url` (voir plus bas).
from werkzeug.middleware.proxy_fix import ProxyFix

# CORRECTIF NOBELLIS : Branchement du vrai moteur d'analyse (Monte Carlo, 10 000 simulations),
# auparavant jamais utilisé. generer_analyse_ia() ci-dessous devient un adaptateur qui appelle
# ce moteur et traduit son résultat vers exactement les champs attendus par le template.
import algorithme
# AJOUT NOBELLIS (automatisation quotidienne, 10/09/2026) : importé une seule fois au démarrage -
# voir _boucle_synchronisation_api_quotidienne plus bas. Cet import ne fait JAMAIS planter le
# serveur même si cles_api.py est absent ou corrompu (voir le correctif dans ce module même :
# l'ancien sys.exit(1) à l'import a été déplacé pour ne jamais tuer main.py) - dans ce cas, la
# boucle journalisera l'erreur à chaque tentative quotidienne et continuera d'exister sans crasher.
import synchroniser_matchs_api
# AJOUT NOBELLIS (Bloc 1, classements réels, 11/09/2026) : même principe que
# synchroniser_matchs_api - importé une seule fois, jamais de crash au démarrage même sans
# cles_api.py (le module gère lui-même l'absence de clé via FOOTBALL_DATA_API_KEY = None).
import synchroniser_classements_api
# AJOUT NOBELLIS (Bloc 2, résultats réels + forme, 12/09/2026) : même principe que les deux
# imports précédents - jamais de crash au démarrage même sans cles_api_football.py (le module
# gère lui-même l'absence de clé via API_FOOTBALL_KEY = None).
import synchroniser_resultats_api_football
import head_to_head_api_football
# AJOUT NOBELLIS (26/09/2026, chantier "périodes creuses") : crée des matchs pour 4 compétitions
# supplémentaires (UEFA Nations League, CAN, Allsvenskan, Eliteserien) via API-Football, en plus
# des 12 championnats football-data.org existants - jamais à la place. Même principe de
# robustesse à l'import que les 3 modules ci-dessus : jamais de crash au démarrage même sans
# cles_api_football.py.
import synchroniser_nouvelles_competitions_api_football

# AJOUT NOBELLIS : module de persistance Firestore (API REST + compte de service, sans
# `firebase-admin` pour rester compatible Pydroid 3 - voir firestore_client.py pour le détail).
# Si ce module échoue à se connecter pour une raison quelconque, l'app continue de fonctionner
# entièrement en mémoire, exactement comme avant son ajout - jamais de dépendance bloquante.
import firestore_client

# =========================================================================
# CONFIGURATION ET FORÇAGE DU RÉPERTOIRE DE TRAVAIL (MOBILE-FIRST / PYDROID 3)
# =========================================================================
REAL_PATH: str = os.path.dirname(os.path.abspath(__file__))
os.chdir(REAL_PATH)

if REAL_PATH not in sys.path:
    sys.path.insert(0, REAL_PATH)

TEMPLATE_DIR: str = os.path.join(REAL_PATH, 'templates')
STATIC_DIR: str = os.path.join(REAL_PATH, 'static')

# CORRECTIF NOBELLIS : constante module-level (lue une seule fois au démarrage) qui contrôle
# l'affichage du bandeau de diagnostic "is_analyst (serveur) = ..." dans l'onglet Profil.
# Masqué par défaut pour tout utilisateur final - un outil de diagnostic interne n'a rien à
# faire visible en production. S'active uniquement via la variable d'environnement
# NOBELLIS_DEBUG_DIAGNOSTIC=1, à définir manuellement par l'exploitant en cas de besoin
# d'investigation, jamais par défaut ni via une action utilisateur dans l'app.
DEBUG_DIAGNOSTIC_ACTIF: bool = os.environ.get("NOBELLIS_DEBUG_DIAGNOSTIC", "0") == "1"

app: Flask = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)

# ========================================================================
# AJOUT NOBELLIS (comptes séparés, Étape 1 - fondation seule, 16/09/2026)
# ========================================================================
# ÉTAPE 1 UNIQUEMENT : met en place le mécanisme d'identification stable de chaque visiteur.
# NE TOUCHE À AUCUN des usages existants de DATA_PROFIL (singulier) - le site continue de
# fonctionner exactement comme avant. L'Étape 2 (chantier séparé, non commencé) migrera route
# par route vers ce nouveau système, après un audit précis du format de profil réellement
# nécessaire - jamais deviné à l'avance.
DOC_CLE_SESSION_FLASK = "cle_session_flask"
FICHIER_CLE_SESSION_LOCAL = os.path.join(os.path.expanduser("~"), "nobellis_session_key.txt")


def obtenir_cle_stable(doc_id: str, fichier_local: str, libelle: str) -> str:
    """
    CORRECTIF NOBELLIS (généralisation, 16/09/2026) : logique unique réutilisée pour TOUTE clé
    secrète qui doit survivre aux redémarrages - la clé de session Flask (obtenir_cle_secrete_stable
    ci-dessous) ET, depuis aujourd'hui, ADMIN_SECRET_KEY (voir plus bas) - jamais deux copies
    divergentes de la même logique pour deux clés différentes.

    Ordre de résolution strict, jamais l'inverse :
    1. Firestore (partagée, survit même à un changement d'appareil)
    2. Fichier local sur l'appareil (secours si Firestore injoignable - même principe déjà
       établi par POIDS_FILE dans algorithme.py, précédent existant sur ce projet, pas une
       nouvelle idée improvisée)
    3. Génération UNIQUE si aucune des deux n'existe encore, puis sauvegarde IMMÉDIATE dans les
       deux - tous les redémarrages futurs, avec ou sans connexion, retrouveront cette même clé.
    """
    doc = firestore_client.lire_document(doc_id)
    if doc and doc.get("cle"):
        return doc["cle"]

    if os.path.exists(fichier_local):
        try:
            with open(fichier_local, "r", encoding="utf-8") as f:
                cle_locale = f.read().strip()
            if cle_locale:
                # Trouvée en local mais absente de Firestore (ex: une écriture précédente a
                # échoué faute de réseau) - retente de la propager maintenant, sans bloquer le
                # démarrage si Firestore est encore injoignable.
                firestore_client.ecrire_document(doc_id, {"cle": cle_locale})
                return cle_locale
        except Exception as erreur:
            print(f"⚠️  [NOBELLIS] Fichier de clé ({libelle}) local illisible, ignoré : {erreur}", file=sys.stderr)

    nouvelle_cle = secrets.token_hex(32)
    try:
        with open(fichier_local, "w", encoding="utf-8") as f:
            f.write(nouvelle_cle)
    except Exception as erreur:
        print(f"⚠️  [NOBELLIS] Impossible d'écrire la clé ({libelle}) en local : {erreur}", file=sys.stderr)
    firestore_client.ecrire_document(doc_id, {"cle": nouvelle_cle})
    print(f"🔐 [NOBELLIS] Nouvelle clé stable générée ({libelle}) (Firestore + secours local) - "
          f"ne changera plus jamais aux prochains démarrages.", file=sys.stderr)
    return nouvelle_cle


def obtenir_cle_secrete_stable() -> str:
    """
    Clé secrète Flask STABLE, jamais régénérée à chaque démarrage. Une clé qui changerait
    romprait silencieusement l'identité de session de TOUS les visiteurs à chaque redémarrage
    (cookie signé avec une ancienne clé = invalide, nouveau visiteur anonyme recréé sans le
    savoir) - jamais acceptable pour ce mécanisme. Voir obtenir_cle_stable() ci-dessus pour le
    détail de la résolution (Firestore -> fichier local -> génération unique).
    """
    return obtenir_cle_stable(DOC_CLE_SESSION_FLASK, FICHIER_CLE_SESSION_LOCAL, "session Flask")


app.secret_key = obtenir_cle_secrete_stable()
# AJOUT NOBELLIS (diagnostic temporaire, 16/09/2026) : empreinte courte de la clé de session -
# JAMAIS la vraie clé (secrets.token_hex(32), 64 caractères) - juste un petit résumé (8
# caractères d'un hachage) qui permet de VÉRIFIER si la clé change entre deux démarrages, sans
# jamais l'exposer. Si cette empreinte est identique à chaque redémarrage, la clé est bien
# stable et le problème de "nouveau visiteur à chaque fois" vient d'ailleurs (pas de cette clé).
print(f"🔎 [NOBELLIS] Empreinte de la clé de session (diagnostic uniquement, jamais la vraie clé) : "
      f"{hashlib.sha256(app.secret_key.encode('utf-8')).hexdigest()[:8]}", file=sys.stderr)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)  # Un visiteur reste reconnu 1 an, même après avoir fermé son navigateur.

# AJOUT NOBELLIS (comptes séparés, Étape 1, 16/09/2026) : conteneur vide, PAS ENCORE utilisé par
# aucune route existante - voir l'avertissement en tête de cette section.
DATA_PROFILS: Dict[str, Dict[str, Any]] = {}


def obtenir_visiteur_id() -> str:
    """
    Lit l'identifiant du visiteur courant depuis son cookie de session Flask (signé avec la clé
    stable ci-dessus - jamais falsifiable sans la connaître), en crée un nouveau et le persiste
    dans la session si c'est sa première visite. Jamais deviné, jamais partagé entre deux
    visiteurs différents - chaque nouvel identifiant est un UUID aléatoire unique.
    """
    if "visiteur_id" not in session:
        session["visiteur_id"] = str(uuid.uuid4())
        session.permanent = True
    return session["visiteur_id"]


NOM_COLLECTION_PROFILS_VISITEURS = "nobellis_profils_visiteurs"  # Vraie collection, jamais un document unique - même principe déjà appliqué 3 fois aujourd'hui (matchs, stats détaillées, confrontations directes).


def sauvegarder_profil_visiteur(visiteur_id: str, profil: Dict[str, Any]) -> None:
    """
    AJOUT NOBELLIS (comptes séparés, Lot 1, 16/09/2026) : persiste UN profil précis - jamais
    toute la collection réécrite pour une seule mise à jour. "visiteur_id" stocké comme CHAMP
    du document (indispensable : _lire_collection_avec_etat ne renvoie jamais l'identifiant
    Firestore du document lui-même, seulement son contenu - même piège déjà rencontré et corrigé
    pour les confrontations directes ce matin).
    """
    document = dict(profil)
    document["visiteur_id"] = visiteur_id
    firestore_client.ecrire_documents_par_lot(NOM_COLLECTION_PROFILS_VISITEURS, {visiteur_id: document})


def charger_profils_visiteurs_depuis_firestore() -> Dict[str, Dict[str, Any]]:
    """À appeler UNE FOIS au démarrage - jamais à chaque visite. Lit toute la collection en un
    seul appel (pagination automatique)."""
    _, documents = firestore_client._lire_collection_avec_etat(
        NOM_COLLECTION_PROFILS_VISITEURS, tentatives=firestore_client._TENTATIVES_LECTURE_DEMARRAGE
    )
    return {d["visiteur_id"]: d for d in documents if d.get("visiteur_id")}


def obtenir_profil_courant() -> Dict[str, Any]:
    """
    AJOUT NOBELLIS (comptes séparés, Lot 1, 16/09/2026) : retourne le profil du visiteur actuel
    dans DATA_PROFILS (pluriel), jamais DATA_PROFIL (singulier, ancien système, entièrement
    remplacé depuis le Lot 4).

    Sécurité vérifiée (audit du 16/09/2026) :
    - copy.deepcopy() du modèle, JAMAIS une copie simple - un modèle avec une liste
      (fiches_deja_vues) copié superficiellement ferait partager la MÊME liste entre tous les
      visiteurs, une vraie fuite de données entre deux personnes différentes.
    - Le champ "id" n'est JAMAIS hérité du modèle (qui contiendrait un id figé, calculé une
      seule fois au chargement du fichier) - toujours régénéré frais pour CE visiteur précis.
    - Protégé par _VERROU_MUTATION_ETAT (même verrou que DATA_MATCHS/DATA_ANALYSTES) - jamais
      une création de profil en double si deux requêtes du même nouveau visiteur arrivent
      quasi simultanément (ex: la page charge plusieurs ressources en parallèle).

    CORRECTIF NOBELLIS (audit, vérification ciblée, 16/09/2026) : le chargement en lot au
    démarrage (charger_profils_visiteurs_depuis_firestore) peut échouer intégralement - un aléa
    réseau passager, ou une vraie panne prolongée comme le quota Firestore épuisé observé
    aujourd'hui - sans qu'aucune exception ne remonte (par design, voir sa documentation).
    Résultat sans ce correctif : DATA_PROFILS démarre vide, et un visiteur qui revient après un
    redémarrage est traité comme un parfait inconnu alors que son vrai profil existe toujours
    sur Firestore - PIRE : une modification qu'il ferait dans cet état écraserait silencieusement
    son vrai profil (même identifiant, même document Firestore). Avant de conclure "profil jamais
    vu", une dernière vérification CIBLÉE sur SON identifiant précis - jamais une deuxième lecture
    de toute la collection, seulement ce visiteur.

    La vérification réseau se fait HORS du verrou (jamais un appel bloquant pendant que _VERROU_
    MUTATION_ETAT est tenu - ça figerait TOUS les visiteurs le temps de la requête Firestore).
    Le verrou est repris ensuite avec une seconde vérification anti-course (double-checked
    locking) : si un autre thread a déjà créé ce profil entre-temps, on ne le recrée jamais.
    """
    visiteur_id = obtenir_visiteur_id()

    with _VERROU_MUTATION_ETAT:
        if visiteur_id in DATA_PROFILS:
            return DATA_PROFILS[visiteur_id]

    # Hors du verrou, volontairement - voir la documentation ci-dessus.
    profil_distant = firestore_client.lire_document_dans_collection(NOM_COLLECTION_PROFILS_VISITEURS, visiteur_id)

    with _VERROU_MUTATION_ETAT:
        if visiteur_id in DATA_PROFILS:
            # Anti-course : un autre thread a pu le créer/récupérer pendant la vérification
            # ci-dessus - jamais un second profil recréé par-dessus dans ce cas.
            return DATA_PROFILS[visiteur_id]

        if profil_distant:
            # Le vrai profil existait déjà sur Firestore, juste raté par le chargement en lot -
            # récupéré maintenant, jamais recréé vide, jamais de perte silencieuse.
            DATA_PROFILS[visiteur_id] = profil_distant
        else:
            nouveau_profil = copy.deepcopy(DATA_PROFIL_MODELE_PAR_DEFAUT)
            nouveau_profil["id"] = f"USR-{secrets.token_hex(6).upper()}"
            DATA_PROFILS[visiteur_id] = nouveau_profil
            sauvegarder_profil_visiteur(visiteur_id, nouveau_profil)

    return DATA_PROFILS[visiteur_id]

# CORRECTIF NOBELLIS (audit, faille n°1) : url_for('static', filename=...) fabrique TOUJOURS une
# adresse locale du type "/static/...". Avant ce filtre, un logo fourni comme URL complète par une
# future API football (ex: "https://media.api-sports.io/football/teams/85.png") aurait été
# transformé en "/static/https://media.api-sports.io/..." - une adresse cassée qui n'affiche
# jamais rien. Ce filtre distingue les deux cas : une URL http(s) est utilisée telle quelle,
# un chemin local (ex: "logos/psg.png") continue de passer par url_for('static', ...) exactement
# comme avant, pour ne rien casser sur les logos de démonstration actuels.
@app.template_filter('logo_url')
def filtre_logo_url(chemin_logo):
    valeur = str(chemin_logo or "").strip()
    if not valeur:
        valeur = "logos/placeholder.png"
    if valeur.startswith("http://") or valeur.startswith("https://"):
        return valeur
    chemin_local = valeur.replace("/static/", "").replace("static/", "")
    return url_for("static", filename=chemin_local)

# CORRECTIF NOBELLIS : horodatage de démarrage du processus, calculé une seule fois au chargement
# du module - sert de "preuve de version" fiable et automatique (jamais oubliée à mettre à jour
# manuellement, contrairement à un numéro de version codé en dur). Combiné au PID déjà affiché au
# démarrage, permet de vérifier en quelques secondes - via le bloc diagnostic du Profil ou via
# /api/admin/diagnostic-profil - que le code réellement exécuté correspond bien à la dernière
# livraison, sans dépendre d'une supposition sur l'état du cache du navigateur.
HEURE_DEMARRAGE_SERVEUR: str = datetime.now(timezone.utc).isoformat()

# CORRECTIF NOBELLIS : limite globale de taille de requête (défense en profondeur). Même si
# un futur endpoint oubliait une vérification de taille manuelle, cette limite bloque toute
# requête trop volumineuse avant même qu'elle atteigne le code métier. 3 Mo couvre confortablement
# l'avatar encodé en base64 (2 Mo max, +33% environ à cause de l'encodage) plus les autres champs du formulaire.
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024

# CORRECTIF NOBELLIS : Derrière un proxy/reverse-proxy (nginx, load balancer), request.remote_addr
# renverrait l'IP du proxy pour toutes les requêtes (rate-limiting inutile ou bloquant tout le monde).
# ProxyFix restaure la vraie IP cliente à partir de l'en-tête X-Forwarded-For standard.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

# CORRECTIF NOBELLIS (audit, cause racine du "profil qui redevient neuf", 16/09/2026) : cette
# ligne réassignait SILENCIEUSEMENT app.secret_key à une clé aléatoire À CHAQUE DÉMARRAGE
# ("Régénération automatique au boot" - légitime avant l'existence des comptes séparés, mais
# jamais mis à jour depuis), écrasant purement et simplement le réglage stable déjà fait plus
# haut (voir app.secret_key = obtenir_cle_secrete_stable(), ligne ~140). Conséquence exacte du
# problème signalé aujourd'hui : la clé affichée par le diagnostic semblait stable (imprimée
# AVANT cette ligne), mais la vraie clé utilisée pour signer les cookies changeait quand même à
# chaque redémarrage - invalidant tous les cookies existants, donc tous les visiteurs revenaient
# "inconnus". Supprimée - obtenir_cle_secrete_stable() est désormais la SEULE source de vérité.

# =========================================================================
# CORRECTIF NOBELLIS : CLÉ D'ACCÈS ADMINISTRATEUR (VERROUILLAGE DES ROUTES SENSIBLES)
# =========================================================================
# Tant qu'aucune vraie base d'utilisateurs avec rôles n'existe (prévue dans la version
# suivante avec base de données), toute action admin (création/clôture de match) exige
# cette clé secrète en en-tête HTTP "X-Admin-Key". Sans elle : accès refusé, sans exception.
# Définissable via la variable d'environnement ADMIN_SECRET_KEY pour un déploiement réel ;
# CORRECTIF NOBELLIS (clé admin stable, 16/09/2026) : sinon (pas de variable d'environnement),
# utilise désormais le même mécanisme déjà éprouvé pour la clé de session (Firestore + secours
# local + génération unique) - jamais plus une nouvelle clé aléatoire à chaque redémarrage. Le
# précédent comportement ("changée à chaque démarrage") n'était pas un choix de sécurité
# volontaire, seulement jamais corrigé jusqu'ici - inutile de re-communiquer une nouvelle clé
# à chaque fois quand une clé stable + protégée par comparaison en temps constant suffit.
DOC_CLE_ADMIN = "cle_admin_stable"
FICHIER_CLE_ADMIN_LOCAL = os.path.join(os.path.expanduser("~"), "nobellis_admin_key.txt")

ADMIN_SECRET_KEY: str = os.environ.get("ADMIN_SECRET_KEY", "")
if not ADMIN_SECRET_KEY:
    ADMIN_SECRET_KEY = obtenir_cle_stable(DOC_CLE_ADMIN, FICHIER_CLE_ADMIN_LOCAL, "admin")
    print("\n" + "=" * 70, file=sys.stderr)
    print("🔐 [NOBELLIS] Aucune ADMIN_SECRET_KEY définie en variable d'environnement.", file=sys.stderr)
    print(f"🔐 [NOBELLIS] Clé admin stable (ne change plus à chaque démarrage) : {ADMIN_SECRET_KEY}", file=sys.stderr)
    print("🔐 [NOBELLIS] Utilisez-la dans l'en-tête 'X-Admin-Key' de vos requêtes admin.", file=sys.stderr)
    print("🔐 [NOBELLIS] Définissez ADMIN_SECRET_KEY en production pour la remplacer par la vôtre.", file=sys.stderr)
    print("=" * 70 + "\n", file=sys.stderr)

def verifier_cle_admin() -> bool:
    """
    CORRECTIF NOBELLIS : Contrôle d'accès strict aux routes administrateur.
    Comparaison en temps constant pour éviter les attaques par mesure de timing.
    """
    cle_fournie = request.headers.get("X-Admin-Key", "")
    return secrets.compare_digest(cle_fournie, ADMIN_SECRET_KEY)

# CORRECTIF NOBELLIS : liste fermée des spécialités valides. Avant, "specialite" était du texte
# libre validé uniquement sur sa longueur - un analyste pouvait écrire "Rien", un mot inventé ou
# du contenu inapproprié, affiché publiquement sur sa fiche. Toute valeur envoyée par le client
# (formulaire normal OU requête directe contournant l'interface) est désormais vérifiée contre
# cette liste - jamais une confiance aveugle en ce que le navigateur prétend avoir envoyé.
SPECIALITES_AUTORISEES: set = {
    "Ligue 1", "Premier League", "Liga", "Bundesliga", "Serie A",
    "Ligue des Champions", "Ligue Europa", "Coupe du Monde"
}


def valider_specialite(valeur_brute: str) -> str:
    """
    Découpe une chaîne "A & B & C" en éléments individuels, ne conserve que ceux figurant dans
    SPECIALITES_AUTORISEES, et rejoint le résultat filtré. Toute valeur absente de la liste est
    silencieusement écartée plutôt que de faire échouer toute la requête sur un seul élément
    invalide - mais rien d'inconnu n'est jamais conservé.
    """
    elements_bruts = [e.strip() for e in str(valeur_brute).split("&")]
    elements_valides = [e for e in elements_bruts if e in SPECIALITES_AUTORISEES]
    return " & ".join(elements_valides)


def verifier_integrite_specialites_demo() -> None:
    """
    CORRECTIF NOBELLIS : les données de démonstration (DATA_ANALYSTES) sont codées en dur au
    démarrage et ne passent JAMAIS par valider_specialite() - contrairement aux formulaires
    utilisateur, rien ne les protège automatiquement contre une spécialité invalide ou mal
    orthographiée ajoutée par erreur dans le code plus tard (c'est exactement ce qui s'est
    produit avec "BUNDESLIGA & STATS XG", jamais filtré, affiché tel quel aux utilisateurs).
    Cette fonction ne se contente pas de signaler le problème dans les logs - un simple log au
    démarrage n'est visible que sur le terminal du développeur, jamais par l'utilisateur final
    sur Pydroid3, donc une donnée corrompue continuerait d'être servie malgré la détection.
    Elle CORRIGE la donnée en mémoire avant que le serveur ne commence à répondre aux requêtes,
    et journalise chaque correction effectuée pour que le développeur en soit informé sans que
    l'utilisateur final n'ait jamais à voir la version non filtrée.
    """
    for analyste in DATA_ANALYSTES:
        valeur_originale = analyste.get("specialite", "")
        valeur_filtree = valider_specialite(valeur_originale)
        if valeur_filtree != valeur_originale:
            print(
                f"⚠️  [NOBELLIS] Spécialité de démo invalide corrigée pour "
                f"'{analyste.get('id', '?')}' : \"{valeur_originale}\" → \"{valeur_filtree}\"",
                file=sys.stderr
            )
            analyste["specialite"] = valeur_filtree


def verifier_integrite_horodatages_fiches_demo() -> None:
    """
    CORRECTIF NOBELLIS : les fiches de démonstration codées en dur (liste_pronostics des
    analystes de démo) ont été créées avant l'existence du champ "publie_le" - sans ce filet de
    sécurité, l'affichage de l'heure de publication resterait vide sur ces cartes précisément,
    visibles dès la première ouverture de l'app. Attribution d'horodatages plausibles et
    échelonnés (pas tous identiques) pour un rendu crédible dès le démarrage.
    """
    maintenant = datetime.now(timezone.utc)
    decalage_heures = 3
    for analyste in DATA_ANALYSTES:
        for fiche in analyste.get("liste_pronostics", []):
            if not fiche.get("publie_le"):
                fiche["publie_le"] = (maintenant - timedelta(hours=decalage_heures)).isoformat()
                print(
                    f"⚠️  [NOBELLIS] Horodatage de publication manquant comblé pour la fiche "
                    f"'{fiche.get('id', '?')}' de '{analyste.get('id', '?')}'.",
                    file=sys.stderr
                )
                decalage_heures += 5

# =========================================================================
# SYSTEME DE SÉCURITÉ RESSOURCE : RATELIMITER CIRCULAIRE FIXE (ANTI-OOM MOBILE)
# =========================================================================
RATE_LIMIT_CAPACITY: int = 4096
RATE_LIMIT_BUFFER: Dict[str, List[float]] = {}
RATE_LIMIT_KEYS_ORDER: List[str] = []

def verifier_rate_limit(ip_client: str, max_requetes: int = 15, fenetre_secondes: float = 1.0) -> bool:
    """
    Contrôle de flux à capacité fixe avec éviction FIFO stricte.
    Garantit l'intégrité de la mémoire vive sous haute charge.
    """
    global RATE_LIMIT_BUFFER, RATE_LIMIT_KEYS_ORDER
    temps_actuel = time.time()
    
    if ip_client not in RATE_LIMIT_BUFFER:
        if len(RATE_LIMIT_KEYS_ORDER) >= RATE_LIMIT_CAPACITY:
            ancienne_ip = RATE_LIMIT_KEYS_ORDER.pop(0)
            RATE_LIMIT_BUFFER.pop(ancienne_ip, None)
        RATE_LIMIT_BUFFER[ip_client] = []
        RATE_LIMIT_KEYS_ORDER.append(ip_client)
        
    timestamps = RATE_LIMIT_BUFFER[ip_client]
    timestamps = [t for t in timestamps if temps_actuel - t < fenetre_secondes]
    RATE_LIMIT_BUFFER[ip_client] = timestamps
    
    if len(timestamps) >= max_requetes:
        return False
        
    RATE_LIMIT_BUFFER[ip_client].append(temps_actuel)
    return True
# =========================================================================
# MATRICE DES DONNÉES MAÎTRESSES : GRILLE DES MATCHS ACTIFS DE LA SEMAINE
# =========================================================================
DATA_MATCHS: List[Dict[str, Any]] = [
    {
        "id": "1",
        "competition": "Ligue 1 McDonald's",
        "slug": "ligue1mcdonalds",
        "heure": "21:00",
        "home": "Paris SG",
        "away": "Marseille",
        "home_logo_url": "logos/psg.png",
        "away_logo_url": "logos/om.png",
        "tendance": "true",
        "type": "officiel"
    },
    {
        "id": "2",
        "competition": "Premier League",
        "slug": "premierleague",
        "heure": "18:30",
        "home": "Arsenal",
        "away": "Manchester City",
        "home_logo_url": "logos/arsenal.png",
        "away_logo_url": "logos/mancity.png",
        "tendance": "true",
        "type": "officiel"
    },
    {
        "id": "3",
        "competition": "Bundesliga",
        "slug": "bundesliga",
        "heure": "15:30",
        "home": "Bayern Munich",
        "away": "Dortmund",
        "home_logo_url": "logos/bayern.png",
        "away_logo_url": "logos/bvb.png",
        "tendance": "false",
        "type": "officiel"
    }
]
# Suite de la collection maîtresse DATA_MATCHS
DATA_MATCHS.extend([
    {
        "id": "4",
        "competition": "Copa del Rey",
        "slug": "copadelrey",
        "heure": "22:00",
        "home": "Real Madrid",
        "away": "FC Barcelone",
        "home_logo_url": "logos/real.png",
        "away_logo_url": "logos/barca.png",
        "tendance": "true",
        "type": "officiel"
    },
    {
        "id": "5",
        "competition": "UEFA Champions League",
        "slug": "uefachampionsleague",
        "heure": "21:00",
        "home": "Real Madrid",
        "away": "Manchester City",
        "home_logo_url": "logos/real.png",
        "away_logo_url": "logos/mancity.png",
        "tendance": "true",
        "type": "officiel"
    },
    {
        "id": "6",
        "competition": "LaLiga EA Sports",
        "slug": "laligaeasports",
        "heure": "16:15",
        "home": "FC Barcelone",
        "away": "Atlético Madrid",
        "home_logo_url": "logos/barca.png",
        "away_logo_url": "logos/atletico.png",
        "tendance": "false",
        "type": "officiel"
    },
    {
        "id": "7",
        "competition": "Match Amical",
        "slug": "matchamical",
        "heure": "20:45",
        "home": "France",
        "away": "Allemagne",
        "home_logo_url": "logos/france.png",
        "away_logo_url": "logos/allemagne.png",
        "tendance": "true",
        "type": "amical"
    }
])

# CORRECTIF NOBELLIS : Enrichissement anti-fraude de tous les matchs existants.
for _match in DATA_MATCHS:
    _match.setdefault("kickoff_utc", "2027-01-01T21:00:00+00:00")
    _match.setdefault("statut", "a_venir")
    _match.setdefault("score_final", None)
    _match.setdefault("cloture_timestamp", None)
    _match.setdefault("corrections_score", [])
    # CORRECTIF NOBELLIS (audit, faille n°8) : étiquette invisible distinguant les matchs de
    # démonstration des vrais matchs qui viendront d'une API football. Permet un remplacement
    # automatique et propre (voir obtenir_matchs_visibles plus bas) le jour où l'API est branchée,
    # sans avoir à supprimer manuellement les matchs de test.
    _match.setdefault("source", "demo")

# CORRECTIF NOBELLIS : diversification des heures de démonstration - calculée dynamiquement au
# démarrage (jamais une date écrite en dur qui deviendrait obsolète), pour montrer les différents
# états réels (à venir / résultat en attente / terminé / annulé) dès la première ouverture de
# l'app. Les matchs 1, 2 et 3 restent volontairement intacts et "à venir" : ce sont les seuls
# liés à une fiche d'analyste de démonstration (p_101, p_102, p_103) - les faire passer à
# "terminé" ou "coup d'envoi dépassé" ferait disparaître ces fiches via la règle des 30 minutes,
# effet de bord repéré et évité avant d'agir.
_maintenant_demo = datetime.now(timezone.utc)
_matchs_par_id = {m["id"]: m for m in DATA_MATCHS}

if "1" in _matchs_par_id:
    _matchs_par_id["1"]["kickoff_utc"] = (_maintenant_demo + timedelta(hours=2)).isoformat()
if "2" in _matchs_par_id:
    _matchs_par_id["2"]["kickoff_utc"] = (_maintenant_demo + timedelta(hours=5)).isoformat()
if "3" in _matchs_par_id:
    _matchs_par_id["3"]["kickoff_utc"] = (_maintenant_demo + timedelta(days=1)).isoformat()

if "4" in _matchs_par_id:
    # Coup d'envoi passé, pas encore clôturé -> démontre l'état "Résultat en attente".
    _matchs_par_id["4"]["kickoff_utc"] = (_maintenant_demo - timedelta(minutes=15)).isoformat()

if "5" in _matchs_par_id:
    # Terminé récemment, encore dans la fenêtre de 5 minutes -> visible avec son score.
    _matchs_par_id["5"]["kickoff_utc"] = (_maintenant_demo - timedelta(hours=3)).isoformat()
    _matchs_par_id["5"]["statut"] = "termine"
    _matchs_par_id["5"]["score_final"] = "2-1"
    _matchs_par_id["5"]["cloture_timestamp"] = (_maintenant_demo - timedelta(minutes=2)).isoformat()

if "6" in _matchs_par_id:
    _matchs_par_id["6"]["kickoff_utc"] = (_maintenant_demo + timedelta(days=3)).isoformat()

if "7" in _matchs_par_id:
    # Annulé récemment, encore dans la fenêtre de 5 minutes -> démontre l'état "ANNULÉ".
    _matchs_par_id["7"]["kickoff_utc"] = (_maintenant_demo - timedelta(days=1)).isoformat()
    _matchs_par_id["7"]["statut"] = "annule"
    _matchs_par_id["7"]["cloture_timestamp"] = (_maintenant_demo - timedelta(minutes=2)).isoformat()

# Le champ 'heure' (pré-calculé, affiché brièvement avant la conversion JS en heure locale) est
# recalculé pour rester cohérent avec les nouveaux horodatages ci-dessus.
for _match in DATA_MATCHS:
    try:
        _match["heure"] = datetime.fromisoformat(str(_match["kickoff_utc"])).strftime("%H:%M")
    except (ValueError, TypeError):
        pass
# =========================================================================
# =========================================================================
# CORRECTIF NOBELLIS : PROFIL VISITEUR PAR DÉFAUT - UN VRAI UTILISATEUR SIMPLE
# =========================================================================
# Auparavant, chaque personne ouvrant l'app pour la première fois héritait automatiquement
# de l'identité complète de "Kev_Tactical" (déjà Analyste Élite, avec historique et jetons de
# démo) - aucun véritable parcours "nouvel utilisateur" n'existait. Ça rendait invisible tout
# le travail de certification méritée (Wilson) et de cycle réversible analyste construit
# ailleurs dans cette app : personne n'y passait jamais, puisque tout le monde démarrait déjà
# analyste. Plus grave : le jour où de vrais comptes seront branchés, ce défaut aurait fait de
# CHAQUE nouvelle inscription un analyste automatique sans mérite ni clic - une faille de fraude
# pire que celle déjà fermée ailleurs (qui exigeait au moins un clic explicite).
#
# Kev_Tactical et son historique restent du contenu de démonstration à part entière, visibles
# dans le classement public (voir DATA_ANALYSTES, id "exp_1") - ils ne sont simplement plus liés
# de force à l'identité de la personne qui vient d'ouvrir l'app.
#
# CORRECTIF NOBELLIS : champ "rang" retiré - texte statique jamais recalculé, jamais affiché
# par le template (vérifié par recherche exhaustive), et en contradiction silencieuse avec le
# vrai badge Wilson calculé sur le profil analyste lié - un champ mort qui mentait sans jamais
# être vu, mais qui aurait pu induire en erreur quiconque lisait directement les données.
DATA_PROFIL: Dict[str, Any] = {
    "analyst_id": None,
    "username": "Nouvel_Utilisateur",
    "nom": "Nouvel_Utilisateur",  # Double ancrage sémantique requis pour l'affichage Jinja2
    "email": "",
    "photo_url": "/static/avatars/placeholder.png",
    "is_analyst": False,
    "progression_suivant": 0,
    "precision_globale": 0,
    "roi_virtuel": 0,
    "meilleure_serie": 0,
    "total_analyses": 0,
    "code_promo": "",
    "max_incertitude": 60,
    "seuil_chaos": 60,  # Alignement avec le filtre de volatilité en temps réel
    "theme": "dark",
    # CORRECTIF NOBELLIS (audit, détection automatique du thème système) : False uniquement pour
    # un profil tout neuf, jamais encore sauvegardé nulle part (voir juste après le chargement
    # Firestore : tout profil DÉJÀ EXISTANT, même sans ce champ, est traité comme "déjà choisi" -
    # jamais de changement d'apparence surprise pour un utilisateur déjà actif).
    "theme_defini_manuellement": False,
    # CORRECTIF NOBELLIS : désactivé par défaut - quand False, le bloc diagnostic n'est écrit
    # dans AUCUNE page envoyée au client (condition Jinja côté serveur, pas un masquage visuel).
    "mode_diagnostic": False,
    "langue": "fr",
    "id": f"USR-{secrets.token_hex(6).upper()}",  # CORRECTIF NOBELLIS : généré, jamais partagé entre visiteurs
    # CORRECTIF NOBELLIS (audit, mise en avant "non-vu") : identifiants des fiches déjà ouvertes
    # par ce visiteur via /api/pronostics/acceder/<id> - alimente la mise en avant temporaire des
    # publications récentes non consultées dans le bloc "Abonnements" (voir route_analystes).
    # Nettoyé automatiquement quand une fiche est remplacée (voir api_publier_analyse_cumulative),
    # pour ne jamais accumuler indéfiniment des identifiants pointant vers des fiches disparues.
    "fiches_deja_vues": [],
    # AJOUT NOBELLIS (comptes séparés, Lot 3, 16/09/2026) : liste des identifiants d'analystes
    # suivis PAR CE visiteur précis - corrige une vraie faille de conception trouvée aujourd'hui :
    # "is_following" était jusqu'ici stocké sur la fiche PARTAGÉE de l'analyste
    # (DATA_ANALYSTES[...]["is_following"]), donc identique pour TOUS les visiteurs - si l'un
    # suivait un analyste, tout le monde le voyait comme "suivi". Ce nouveau champ, propre à
    # chaque profil, remplace cet usage-là ; DATA_ANALYSTES[...]["parrainages"] (le compteur
    # total, une vraie statistique globale légitime) reste, lui, sur la fiche de l'analyste.
    "analystes_suivis": [],
    # CORRECTIF NOBELLIS : "jetons" retiré - l'app est entièrement gratuite, ce champ n'était de
    # toute façon jamais débité ni vérifié nulle part (aucune route ne le contrôlait), c'était un
    # affichage cosmétique laissant croire à une monétisation qui n'existe pas.
}

# AJOUT NOBELLIS (comptes séparés, Lot 1, 16/09/2026) : copie EXACTE et FIGÉE du modèle
# ci-dessus, au moment du chargement du fichier - jamais réutilisée directement (DATA_PROFIL,
# lui, sera écrasé par les vraies données Firestore juste après, voir charger_ou_initialiser) et
# jamais mutée elle-même (obtenir_profil_courant() en fait toujours une deepcopy avant usage,
# jamais une référence directe - sinon modifier le profil d'UN visiteur modifierait le modèle
# lui-même pour tous les FUTURS visiteurs).
DATA_PROFIL_MODELE_PAR_DEFAUT: Dict[str, Any] = copy.deepcopy(DATA_PROFIL)
# =========================================================================
# ARCHIVE COMMUNAUTAIRE DES ANALYSTES CERTIFIÉS ET FICHE PRONOSTICS MAÎTRESSE
# =========================================================================
DATA_ANALYSTES: List[Dict[str, Any]] = [
    {
        "id": "exp_1",
        "nom": "Kev_Tactical",
        "specialite": "Ligue 1 & Premier League",
        "precision": 81,
        "total_posts": 2,
        "parrainages": 1420,
        "photo_url": "avatars/analyst1.png",
        "certifie": True,
        "code_promo": "KEV2026",
        "tendance": "up",
        "is_following": True,
        "liste_pronostics": [
            {
                "id": "p_101", 
                "match_id": "1",
                "match_nom": "Paris SG vs Marseille", 
                "match_home": "Paris SG",
                "match_away": "Marseille",
                "home_logo_url": "logos/psg.png",
                "away_logo_url": "logos/om.png",
                "consensus_pct": 94
            },
            {
                "id": "p_102", 
                "match_id": "2",
                "match_nom": "Arsenal vs Manchester City", 
                "match_home": "Arsenal",
                "match_away": "Manchester City",
                "home_logo_url": "logos/arsenal.png",
                "away_logo_url": "logos/mancity.png",
                "consensus_pct": 88
            }
        ]
    },
    {
        "id": "exp_2",
        "nom": "DataVision",
        "specialite": "Bundesliga & Ligue des Champions",
        "precision": 74,
        "total_posts": 1,
        "parrainages": 890,
        "photo_url": "avatars/analyst2.png",
        "certifie": False,
        "code_promo": "VISION2026",
        "tendance": "down",
        "is_following": False,
        "liste_pronostics": [
            {
                "id": "p_103", 
                "match_id": "3",
                "match_nom": "Bayern Munich vs Dortmund", 
                "match_home": "Bayern Munich",
                "match_away": "Dortmund",
                "home_logo_url": "logos/bayern.png",
                "away_logo_url": "logos/bvb.png",
                "consensus_pct": 79
            }
        ]
    }
]

# CORRECTIF NOBELLIS : Ajout des champs de suivi de résolution réelle des pronostics,
# sans toucher aux champs existants. "precision" reste toujours un nombre (jamais None)
# pour ne pas casser la comparaison Jinja2 déjà en place dans le template (analyste.precision >= 80).
for _analyste in DATA_ANALYSTES:
    _analyste.setdefault("pronostics_resolus", 0)
    _analyste.setdefault("pronostics_gagnes", 0)
    _analyste.setdefault("badge_nobellis", "Certifié" if _analyste.get("pronostics_resolus", 0) >= 10 else "Nouveau")
# =========================================================================
# REPERTOIRE DES PRÉDICTIONS EXPERTS ET HISTORIQUES DES SOUS-MARCHÉS DÉTAILLÉS
# =========================================================================
DATA_PREDICTIONS_EXPERT: Dict[str, Dict[str, Any]] = {
    "p_101": {
        "match_id": "1",
        "expert_id": "exp_1",
        "expert_color": "#00e676",
        "match_nom": "Paris SG vs Marseille",
        "consensus_pct": 94,
        "comment_analyst": (
            "Analyse Tactique : Confrontation de styles équilibrée au milieu de terrain. "
            "Les transitions horizontales détermineront l'ouverture des espaces.\n\n"
            "Indice Physiologique : Les deux effectifs affichent un état de fraîcheur optimal. "
            "Le rythme de jeu devrait rester intense sur l'ensemble des 90 minutes.\n\n"
            "Régulation Arbitrale : Monsieur Letexier possède des statistiques de sévérité élevées. "
            "Le risque de sanctions administratives (cartons) ou de penalties est accentué."
        ),
        "predictions": {
            "market_1n2": "V1",
            "market_dc": "1X",
            "market_btts": "Oui",
            "market_domination": "V1",
            "market_possession": "55%",
            "market_penalty": "Aucun",
            "market_rouges": "Non",
            "market_score": "2-1",
            "market_goals": "OVER_2.5",
            "extended_total_v1": "OVER_1.5",
            "extended_total_v2": "OVER_0.5",
            "extended_total_m1": "OVER_0.5",
            "extended_total_m2": "OVER_1.5",
            "extended_total_v1m1": "OVER_0.5",
            "extended_total_v1m2": "OVER_0.5",
            "extended_total_v2m1": "UNDER_0.5",
            "extended_total_v2m2": "OVER_0.5",
            "extended_corners_match": "OVER_9.5",
            "extended_corners_v1": "OVER_5.5",
            "extended_corners_v2": "OVER_3.5",
            "extended_corners_m1": "OVER_4.5",
            "extended_corners_m2": "OVER_4.5",
            "extended_corners_v1m1": "OVER_2.5",
            "extended_corners_v1m2": "OVER_2.5",
            "extended_corners_v2m1": "OVER_1.5",
            "extended_corners_v2m2": "OVER_1.5",
            "extended_tirs_cadres": "OVER_8.5",
            "extended_tirs_v1": "OVER_5.5",
            "extended_tirs_v2": "OVER_3.5",
            "extended_coups_francs": "OVER_22.5",
            "extended_coups_v1": "OVER_12.5",
            "extended_coups_v2": "OVER_10.5",
            "market_cartons": "OVER_3.5",
            "extended_cartons_v1": "OVER_1.5",
            "extended_cartons_v2": "OVER_1.5"
        }
    }
}
# =========================================================================
# MOTEUR DE SIMULATION ET DE LOGIQUE PRÉDICTIVE (GÉNÉRATEUR NOBELLIS IA)
# =========================================================================
# CORRECTIF NOBELLIS : Poids de l'IA chargés une seule fois au démarrage (mémoire d'apprentissage
# persistée par algorithme.py). Rechargés depuis le disque à chaque appel serait inutilement
# coûteux ; ils ne changent que via le cycle d'apprentissage (branché plus tard avec l'API).
# CORRECTIF NOBELLIS (audit) : verrou anti-collision pour toutes les routes de mutation qui n'en
# avaient aucun (suivre un analyste, quitter le statut d'analyste, créer/clôturer/annuler un match,
# publier une analyse, mettre à jour les paramètres). Séparé de _VERROU_CREATION_ANALYSTE (laissé
# intact, déjà correct) pour ne prendre aucun risque de toucher à une section déjà validée.
# Protège uniquement contre les collisions à l'intérieur d'un seul processus Python - cohérent
# avec le principe déjà affirmé ailleurs dans ce fichier (voir _VERROU_CREATION_ANALYSTE) qu'un
# verrou distribué (Redis, transaction base de données) serait nécessaire en cas de déploiement
# multi-processus, hors de portée tant que l'app reste mono-processus par conception.
_VERROU_MUTATION_ETAT = threading.Lock()


def _synchroniser_firestore_arriere_plan(
    data_profil: Optional[Dict[str, Any]] = None,
    data_analystes: Optional[List[Dict[str, Any]]] = None,
    data_predictions_expert: Optional[Dict[str, Dict[str, Any]]] = None,
    data_matchs: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """
    CORRECTIF NOBELLIS (retour terrain) : l'ancien appel direct `firestore_client.synchroniser(...)`
    effectue de vraies requêtes réseau (jusqu'à 3 écritures séquentielles pour une simple publication
    d'analyse), et la réponse au bouton attendait la fin de CES requêtes avant de s'afficher - direct
    sur mobile, signalé comme "boutons lents". Ce correctif fait exactement la même synchronisation,
    mais dans un thread séparé : le bouton reçoit sa réponse immédiatement, la sauvegarde vers
    Firestore se termine juste après, en coulisse, sans jamais bloquer l'utilisateur.

    IMPORTANT sur la sécurité de cette approche : chaque structure est copiée en profondeur
    (`copy.deepcopy`) AVANT de lancer le thread, pendant qu'on tient encore le verrou de la route
    appelante. Sans cette copie, le thread lirait directement DATA_PROFIL/DATA_ANALYSTES/etc. en
    tâche de fond pendant qu'une action suivante pourrait déjà être en train de les modifier -
    risque d'envoyer vers Firestore un mélange incohérent d'ancien et de nouvel état. Avec la
    copie, le thread ne travaille que sur une photographie figée de l'instant présent, donc
    strictement inoffensif quoi qu'il arrive ensuite dans DATA_PROFIL/DATA_ANALYSTES/etc.
    """
    copie_profil = copy.deepcopy(data_profil) if data_profil is not None else None
    copie_analystes = copy.deepcopy(data_analystes) if data_analystes is not None else None
    copie_predictions = copy.deepcopy(data_predictions_expert) if data_predictions_expert is not None else None
    copie_matchs = copy.deepcopy(data_matchs) if data_matchs is not None else None

    thread_sync = threading.Thread(
        target=firestore_client.synchroniser,
        kwargs={
            "data_profil": copie_profil,
            "data_analystes": copie_analystes,
            "data_predictions_expert": copie_predictions,
            "data_matchs": copie_matchs,
        },
        daemon=True,  # Ne bloque jamais l'arrêt du serveur si une écriture traîne encore.
    )
    thread_sync.start()


# AJOUT NOBELLIS (automatisation quotidienne, 10/09/2026) : 4h du matin UTC - heure creuse pour
# la quasi-totalité des fuseaux horaires des compétitions couvertes, aucune importance critique
# sinon. Changer cette seule constante suffit à décaler l'heure, sans toucher au reste de la
# logique de planification ci-dessous.
HEURE_SYNCHRONISATION_API_QUOTIDIENNE = 4


def _prochaine_heure_synchronisation(maintenant: datetime) -> datetime:
    """
    Calcule le prochain déclenchement à HEURE_SYNCHRONISATION_API_QUOTIDIENNE:00 UTC - aujourd'hui
    si cette heure n'est pas encore passée, sinon demain. Ne renvoie jamais un horaire déjà passé,
    jamais deux déclenchements le même jour.
    """
    candidat = maintenant.replace(hour=HEURE_SYNCHRONISATION_API_QUOTIDIENNE, minute=0, second=0, microsecond=0)
    if candidat <= maintenant:
        candidat += timedelta(days=1)
    return candidat


# CORRECTIF NOBELLIS (24/09/2026, rattrapage au démarrage) : la synchronisation ne partait qu'à
# 04:00 UTC ; si l'application (ou le téléphone) dormait à cette heure, la journée entière était
# perdue sans aucune trace, et rien n'était jamais rattrapé. L'heure de la dernière synchronisation
# réussie est désormais conservée dans Firestore : au démarrage, si elle date de plus de
# DELAI_RATTRAPAGE_SYNCHRO (ou n'existe pas), le cycle est rejoué UNE fois, et jamais plus d'une
# fois par période - redémarrer l'application dix fois pendant des tests ne consomme donc pas le
# quota d'appels API. Le délai de DELAI_AVANT_RATTRAPAGE_SECONDES laisse le module finir de se
# charger : le cycle appelle des fonctions définies plus bas dans ce fichier.
DOC_ETAT_SYNCHRO_QUOTIDIENNE = "etat_synchro_quotidienne"
DELAI_RATTRAPAGE_SYNCHRO = timedelta(hours=20)
DELAI_AVANT_RATTRAPAGE_SECONDES = 90.0
_ETAT_SYNCHRO_QUOTIDIENNE: Dict[str, Any] = {"derniere_reussie_utc": None}


def _rattrapage_synchronisation_necessaire(maintenant: datetime) -> bool:
    """
    Vrai si aucune synchronisation réussie n'est connue, ou si la dernière date de plus de
    DELAI_RATTRAPAGE_SYNCHRO. Ne lève jamais d'exception (Firestore indisponible = aucune trace
    connue = rattrapage, le comportement le plus prudent). Renseigne aussi l'état en mémoire lu
    par la page de santé.
    """
    derniere: Optional[datetime] = None
    try:
        document = firestore_client.lire_document(DOC_ETAT_SYNCHRO_QUOTIDIENNE) or {}
        brut = document.get("derniere_reussie_utc")
        if brut:
            derniere = datetime.fromisoformat(str(brut))
            if derniere.tzinfo is None:
                derniere = derniere.replace(tzinfo=timezone.utc)
    except Exception:
        derniere = None
    _ETAT_SYNCHRO_QUOTIDIENNE["derniere_reussie_utc"] = derniere.isoformat() if derniere else None
    if derniere is None:
        print("🔄 [NOBELLIS] Aucune synchronisation réussie connue - rattrapage au démarrage.", file=sys.stderr)
        return True
    if maintenant - derniere > DELAI_RATTRAPAGE_SYNCHRO:
        print(f"🔄 [NOBELLIS] Dernière synchronisation réussie : {derniere.isoformat()} (plus de "
              f"{DELAI_RATTRAPAGE_SYNCHRO.total_seconds() / 3600:.0f}h) - rattrapage au démarrage.", file=sys.stderr)
        return True
    return False


def _enregistrer_synchronisation_reussie() -> None:
    """Conserve l'heure de la dernière synchronisation réussie (mémoire + Firestore). Jamais d'exception."""
    maintenant_iso = datetime.now(timezone.utc).isoformat()
    _ETAT_SYNCHRO_QUOTIDIENNE["derniere_reussie_utc"] = maintenant_iso
    try:
        if not firestore_client.ecrire_document(DOC_ETAT_SYNCHRO_QUOTIDIENNE, {"derniere_reussie_utc": maintenant_iso}):
            print("⚠️  [NOBELLIS] Heure de la dernière synchronisation non enregistrée dans Firestore.", file=sys.stderr)
    except Exception as erreur:
        print(f"⚠️  [NOBELLIS] Heure de la dernière synchronisation non enregistrée : {erreur}", file=sys.stderr)


def _ajouter_nouveaux_matchs_en_memoire() -> int:
    """
    CORRECTIF NOBELLIS (24/09/2026) : les matchs n'étaient chargés en mémoire qu'au démarrage - ceux
    créés par la synchronisation nocturne restaient invisibles (publication, Accueil, confrontations
    directes) jusqu'au prochain redémarrage manuel. AJOUT SEUL : seuls les identifiants encore
    inconnus de la mémoire sont ajoutés, JAMAIS un match existant remplacé ou modifié (la version
    mémoire est toujours la plus récente : ses statuts partent vers Firestore en arrière-plan).
    Renvoie le nombre de matchs ajoutés ; 0 si Firestore ne répond pas (lecture jamais en exception).
    """
    try:
        matchs_distants = firestore_client.lire_tous_les_matchs()
        if not matchs_distants:
            return 0
        with _VERROU_MUTATION_ETAT:
            ids_connus = {m.get("id") for m in DATA_MATCHS}
            nouveaux = [m for m in matchs_distants if m.get("id") and m.get("id") not in ids_connus]
            DATA_MATCHS.extend(nouveaux)
        return len(nouveaux)
    except Exception as erreur:
        print(f"⚠️  [NOBELLIS] Ajout des nouveaux matchs en mémoire ignoré après erreur : {erreur}", file=sys.stderr)
        return 0


def _boucle_synchronisation_api_quotidienne() -> None:
    """
    CORRECTIF NOBELLIS (automatisation, 10/09/2026) : tourne indéfiniment dans un thread démon
    séparé, sans jamais bloquer ni ralentir le serveur Flask lui-même - les visiteurs de l'app ne
    perçoivent strictement aucune différence pendant qu'une synchronisation a lieu en coulisse.

    Une fois par jour, appelle synchroniser_matchs_api.synchroniser_tous_les_matchs(interactif=False) :
    pas de "oui/non" à répondre, personne n'est là pour ça à 4h du matin - l'envoi vers Firestore se
    fait automatiquement dès qu'au moins un nouveau match est prêt.

    Respecte strictement la limite de 10 requêtes/minute de l'offre gratuite football-data.org : la
    fonction sous-jacente n'est appelée qu'une seule fois par 24h, avec les mêmes pauses de 6
    secondes entre compétitions qu'en usage manuel - l'automatisation ne sollicite jamais l'API
    plus souvent qu'un lancement manuel unique par jour. Cette limite étant un DÉBIT (requêtes par
    minute) et non un quota total, une fois par jour ne l'épuise jamais, quelle que soit la durée
    d'utilisation de l'app (vérifié sur la documentation officielle football-data.org).

    Protection à deux niveaux contre un crash silencieux et définitif de l'automatisation :
    synchroniser_tous_les_matchs() ne laisse déjà remonter aucune exception (son propre
    try/except), et cette boucle en ajoute un second par prudence - si une erreur venait malgré
    tout à s'échapper, elle est journalisée et la boucle retente le lendemain, au lieu de mourir
    pour toujours sans que personne ne s'en aperçoive.
    """
    premier_passage = True
    rattrapage_a_faire = False
    while True:
        try:
            if premier_passage:
                premier_passage = False
                time.sleep(DELAI_AVANT_RATTRAPAGE_SECONDES)
                rattrapage_a_faire = _rattrapage_synchronisation_necessaire(datetime.now(timezone.utc))

            maintenant = datetime.now(timezone.utc)
            prochain_declenchement = _prochaine_heure_synchronisation(maintenant)
            secondes_a_attendre = (prochain_declenchement - maintenant).total_seconds()
            if rattrapage_a_faire:
                rattrapage_a_faire = False  # Une seule fois : un échec attend le créneau normal de 04:00 UTC.
                print("🔄 [NOBELLIS] Synchronisation de rattrapage immédiate (créneau de 04:00 UTC manqué).", file=sys.stderr)
            else:
                print(f"🔄 [NOBELLIS] Prochaine synchronisation API automatique : {prochain_declenchement.isoformat()} "
                      f"(dans {secondes_a_attendre / 3600:.1f}h).", file=sys.stderr)
                time.sleep(max(1.0, secondes_a_attendre))

            print("🔄 [NOBELLIS] Synchronisation API automatique en cours...", file=sys.stderr)
            resultat = synchroniser_matchs_api.synchroniser_tous_les_matchs(interactif=False)
            print(f"🔄 [NOBELLIS] Synchronisation automatique terminée : "
                  f"{resultat.get('nouveaux_matchs', 0)} nouveau(x) match(s), "
                  f"{len(resultat.get('equipes_non_reconnues', []))} nom(s) d'équipe non reconnu(s), "
                  f"envoi={'réussi' if resultat.get('envoye') else 'échoué ou rien à envoyer'}.", file=sys.stderr)

            # CORRECTIF NOBELLIS (24/09/2026) : les nouveaux matchs deviennent visibles sans
            # redémarrage - voir _ajouter_nouveaux_matchs_en_memoire (ajout seul, jamais d'écrasement).
            if resultat.get("nouveaux_matchs", 0) > 0:
                nb_matchs_ajoutes = _ajouter_nouveaux_matchs_en_memoire()
                print(f"🔄 [NOBELLIS] {nb_matchs_ajoutes} nouveau(x) match(s) ajouté(s) en mémoire - aucun redémarrage nécessaire.",
                      file=sys.stderr)

            # AJOUT NOBELLIS (Bloc 1, classements réels) : même créneau quotidien, juste après
            # les matchs - un appel de plus dans le même batch, toujours sous la limite de 10
            # requêtes/minute (12 compétitions pour les matchs + 12 pour les classements + 12
            # pour les résultats par identifiant ci-dessous = 36 appels au total, espacés de 6s
            # chacun = un peu plus de 3½ minutes, toujours largement sous la limite - CORRECTIF
            # NOBELLIS (24/09/2026, Partie 2) : le décompte tenait compte de 24 appels avant
            # l'ajout de la clôture par identifiant football-data.org.
            # appels au total, espacés de 6s chacun = moins de 3 minutes, aucun souci de débit).
            print("🔄 [NOBELLIS] Synchronisation des classements en cours...", file=sys.stderr)
            resultat_classements = synchroniser_classements_api.synchroniser_tous_les_classements(interactif=False)
            print(f"🔄 [NOBELLIS] Synchronisation des classements terminée : "
                  f"{resultat_classements.get('equipes_maj', 0)} équipe(s) à jour, "
                  f"envoi={'réussi' if resultat_classements.get('envoye') else 'échoué ou rien à envoyer'}.", file=sys.stderr)

            # CORRECTIF NOBELLIS (audit, fraîcheur mémoire sans redémarrage, 16/09/2026) :
            # jusqu'ici DATA_CLASSEMENTS n'était rechargé qu'au démarrage du serveur - après
            # cette synchro, l'algorithme continuait de raisonner sur des données vieilles de
            # plusieurs jours jusqu'au prochain redémarrage manuel. Réassignation atomique du
            # nom global (jamais clear()+update() : ça laisserait une fenêtre où un thread
            # concurrent verrait un dict vide) - voir algorithme.obtenir_parametres_match_api,
            # qui relit ce nom global à chaque appel et verra donc immédiatement la mise à jour.
            if resultat_classements.get("envoye"):
                _doc_classements_frais = firestore_client.lire_document(firestore_client.DOC_CLASSEMENTS)
                if _doc_classements_frais and isinstance(_doc_classements_frais.get("items"), dict):
                    global DATA_CLASSEMENTS
                    _nouveau_classements = dict(_doc_classements_frais["items"])
                    _nouveau_classements["_meta_derniere_maj_utc"] = _doc_classements_frais.get("derniere_maj_utc")
                    DATA_CLASSEMENTS = _nouveau_classements
                    print(f"🔄 [NOBELLIS] Classements rechargés en mémoire ({len(_nouveau_classements) - 1} "
                          f"équipe(s)) - aucun redémarrage nécessaire.", file=sys.stderr)

            # AJOUT NOBELLIS (Bloc 2, résultats réels + forme, 12/09/2026) : même créneau
            # quotidien - 12 appels de plus (matchs) + 12 (classements) + 1 (résultats du jour,
            # un seul appel groupé) = 25 appels au total, toujours largement sous la limite de
            # 10/minute de football-data.org, et sous la limite de 100/jour d'API-Football.
            print("🔄 [NOBELLIS] Synchronisation des résultats réels (API-Football) en cours...", file=sys.stderr)
            resultat_resultats = synchroniser_resultats_api_football.synchroniser_resultats(interactif=False)
            print(f"🔄 [NOBELLIS] Synchronisation des résultats terminée : "
                  f"{resultat_resultats.get('matchs_traites', 0)} match(s) intégré(s), "
                  f"envoi={'réussi' if resultat_resultats.get('envoye') else 'échoué ou rien à envoyer'}.", file=sys.stderr)

            # CORRECTIF NOBELLIS (audit, fraîcheur mémoire sans redémarrage, 16/09/2026) :
            # même principe que pour DATA_CLASSEMENTS ci-dessus, appliqué à DATA_FORME_REELLE -
            # réassignation atomique, jamais clear()+update().
            # AJOUT NOBELLIS (26/09/2026, chantier "périodes creuses") : même créneau quotidien,
            # juste après les résultats - 5 appels de plus (1 par jour interrogé, voir
            # NB_JOURS_A_INTERROGER dans synchroniser_nouvelles_competitions_api_football.py),
            # toujours largement sous le quota de 100/jour d'API-Football. AJOUT PUR : ne modifie
            # ni n'appelle jamais la synchronisation football-data.org existante.
            print("🔄 [NOBELLIS] Synchronisation des nouvelles compétitions (Nations League, CAN, "
                  "Allsvenskan, Eliteserien) en cours...", file=sys.stderr)
            resultat_nouvelles_competitions = synchroniser_nouvelles_competitions_api_football.synchroniser_nouvelles_competitions(interactif=False)
            print(f"🔄 [NOBELLIS] Synchronisation des nouvelles compétitions terminée : "
                  f"{resultat_nouvelles_competitions.get('nouveaux_matchs', 0)} nouveau(x) match(s), "
                  f"envoi={'réussi' if resultat_nouvelles_competitions.get('envoye') else 'échoué ou rien à envoyer'}.",
                  file=sys.stderr)
            # Même principe que pour les matchs football-data.org : visible sans redémarrage.
            if resultat_nouvelles_competitions.get("nouveaux_matchs", 0) > 0:
                nb_matchs_ajoutes_nc = _ajouter_nouveaux_matchs_en_memoire()
                print(f"🔄 [NOBELLIS] {nb_matchs_ajoutes_nc} nouveau(x) match(s) (nouvelles compétitions) "
                      f"ajouté(s) en mémoire - aucun redémarrage nécessaire.", file=sys.stderr)

            if resultat_resultats.get("envoye"):
                _doc_forme_fraiche = firestore_client.lire_document(synchroniser_resultats_api_football.DOC_FORME_REELLE)
                if _doc_forme_fraiche and isinstance(_doc_forme_fraiche.get("items"), dict):
                    global DATA_FORME_REELLE
                    _nouvelle_forme = dict(_doc_forme_fraiche["items"])
                    _nouvelle_forme["_meta_derniere_maj_utc"] = _doc_forme_fraiche.get("derniere_maj_utc")
                    DATA_FORME_REELLE = _nouvelle_forme
                    print(f"🔄 [NOBELLIS] Forme réelle rechargée en mémoire ({len(_nouvelle_forme) - 1} "
                          f"équipe(s)) - aucun redémarrage nécessaire.", file=sys.stderr)

            # AJOUT NOBELLIS (Chantier 3, head-to-head, 16/09/2026) : matchs à venir de source
            # "api" uniquement (jamais un match de démonstration) - identifiants_equipes
            # réutilisé depuis resultat_resultats, ZÉRO relecture Firestore supplémentaire.
            print("🔄 [NOBELLIS] Synchronisation des confrontations directes en cours...", file=sys.stderr)
            _matchs_a_venir_pour_h2h = [
                (m["home"], m["away"]) for m in DATA_MATCHS
                if m.get("statut") == "a_venir" and m.get("source") == "api"
            ]
            resultat_h2h = head_to_head_api_football.synchroniser_confrontations_directes(
                _matchs_a_venir_pour_h2h,
                resultat_resultats.get("identifiants_equipes", {}),
                DATA_H2H_CONFRONTATIONS,
                synchroniser_resultats_api_football.CORRESPONDANCE_NOMS_API_FOOTBALL,
            )
            print(f"🔄 [NOBELLIS] Confrontations directes terminées : "
                  f"{resultat_h2h.get('confrontations_traitees', 0)} paire(s) traitée(s)"
                  + (", PLAFOND ATTEINT" if resultat_h2h.get("quota_atteint") else "") + ".", file=sys.stderr)
            if resultat_h2h.get("documents_ecrits"):
                # Fusion EN PLACE des nouveaux documents dans le cache existant - jamais une
                # réassignation totale qui perdrait les entrées déjà en cache mais non retouchées
                # aujourd'hui (contrairement à DATA_CLASSEMENTS/DATA_FORME_REELLE qui sont
                # entièrement remplacés chaque nuit, ce cache grandit de façon incrémentale).
                # .update() mute l'objet existant - jamais besoin de "global" ici, seule une
                # RÉAFFECTATION du nom (DATA_H2H_CONFRONTATIONS = ...) l'exigerait.
                DATA_H2H_CONFRONTATIONS.update(resultat_h2h["documents_ecrits"])

            # AJOUT NOBELLIS (24/09/2026, Partie 2) : clôture par IDENTIFIANT EXACT, AUTORITÉ
            # PRINCIPALE - 12 appels de plus vers football-data.org (un par compétition, mêmes
            # 6s de pause que synchroniser_tous_les_matchs). Isolée dans son propre try/except :
            # une panne ici (ex: football-data indisponible) ne doit JAMAIS empêcher la clôture
            # de repli (nom + date, ci-dessous) de s'exécuter sur les matchs restants.
            resultat_cloture_id: Dict[str, Any] = {"nb_clotures": 0, "nb_sans_score_exploitable": 0}
            try:
                print("🔄 [NOBELLIS] Récupération des résultats par identifiant (football-data.org) en cours...", file=sys.stderr)
                resultats_par_id_api = synchroniser_matchs_api.recuperer_resultats_par_identifiant()
                print(f"🔄 [NOBELLIS] {len(resultats_par_id_api)} résultat(s) par identifiant récupéré(s).", file=sys.stderr)
                resultat_cloture_id = cloturer_matchs_par_identifiant_football_data(resultats_par_id_api)
                print(f"🔄 [NOBELLIS] Clôture par identifiant terminée : "
                      f"{resultat_cloture_id.get('nb_clotures', 0)} match(s) clôturé(s), "
                      f"{resultat_cloture_id.get('nb_sans_score_exploitable', 0)} sans score exploitable.", file=sys.stderr)
            except Exception as erreur_cloture_id:
                print(f"🔄 [NOBELLIS] Clôture par identifiant ignorée après erreur : {erreur_cloture_id} "
                      f"- la clôture de repli (nom + date) ci-dessous prend le relais.", file=sys.stderr)

            # AJOUT NOBELLIS (26/09/2026, "et si le match a été reporté ?") : même créneau
            # quotidien, 12 appels de plus vers football-data.org (un par compétition, mêmes 6s de
            # pause). Isolée dans son propre try/except, comme la clôture par identifiant
            # ci-dessus : une panne ici ne doit jamais empêcher le reste de la synchronisation
            # nocturne (résultats, classements...) de continuer.
            try:
                print("🔄 [NOBELLIS] Recherche de matchs reportés (football-data.org) en cours...", file=sys.stderr)
                matchs_reportes_par_id_api = synchroniser_matchs_api.recuperer_matchs_reportes_par_identifiant()
                print(f"🔄 [NOBELLIS] {len(matchs_reportes_par_id_api)} match(s) reporté(s) trouvé(s) au total.", file=sys.stderr)
                resultat_report = traiter_matchs_reportes_par_identifiant(matchs_reportes_par_id_api)
                print(f"🔄 [NOBELLIS] Traitement des matchs reportés terminé : "
                      f"{resultat_report.get('nb_matchs_reportes', 0)} match(s) marqué(s) reporté(s).", file=sys.stderr)
            except Exception as erreur_report:
                print(f"🔄 [NOBELLIS] Détection des matchs reportés ignorée après erreur : {erreur_report}.", file=sys.stderr)

            # AJOUT NOBELLIS (Bloc 2, clôture automatique, 13/09/2026) : AUCUN appel API
            # supplémentaire ici - matchs_pour_cloture vient d'être construit ci-dessus par
            # synchroniser_resultats() dans ce même cycle (règle 8, quota). Voir
            # cloturer_matchs_automatiquement() pour la règle d'ambiguïté stricte et la
            # correspondance (home, away) - jamais un score deviné.
            # CORRECTIF NOBELLIS (24/09/2026, Partie 2) : REPLI uniquement désormais - ne considère
            # que les matchs encore "à venir" après la clôture par identifiant ci-dessus (matchs
            # sans id_api, ou dont le résultat par identifiant n'était pas encore disponible).
            print("🔄 [NOBELLIS] Clôture de repli (nom + date) des matchs restants en cours...", file=sys.stderr)
            resultat_cloture = cloturer_matchs_automatiquement(resultat_resultats.get("matchs_pour_cloture", []))
            print(f"🔄 [NOBELLIS] Clôture de repli terminée : "
                  f"{resultat_cloture.get('nb_clotures', 0)} match(s) clôturé(s), "
                  f"{resultat_cloture.get('nb_ambigus', 0)} cas ambigu(s), "
                  f"{resultat_cloture.get('nb_hors_date', 0)} sans résultat à la bonne date, laissé(s) pour clôture manuelle.", file=sys.stderr)

            # CORRECTIF NOBELLIS (24/09/2026) : réussite = ni la synchro des matchs ni celle des
            # résultats n'a signalé d'erreur (une panne d'API ne doit pas empêcher un rattrapage).
            if not resultat.get("erreur") and not resultat_resultats.get("erreur"):
                _enregistrer_synchronisation_reussie()
        except Exception as erreur:
            # Filet de sécurité ultime - voir docstring : ne devrait normalement jamais se
            # déclencher, synchroniser_tous_les_matchs()/synchroniser_tous_les_classements()
            # gérant déjà leurs propres erreurs chacune de son côté.
            print(f"🔄 [NOBELLIS] Erreur inattendue dans la boucle de synchronisation automatique : "
                  f"{erreur} - nouvelle tentative prévue demain.", file=sys.stderr)
            time.sleep(3600)  # Évite une boucle d'erreur immédiate en cas de bug répété au démarrage.


_POIDS_IA_ACTUELS: Dict[str, float] = algorithme.charger_poids_ia()

# CORRECTIF NOBELLIS (audit, import historique) : résumés statistiques par équipe, importés
# une seule fois via importer_historique.py (jamais construits ici) - vide tant que cet import
# n'a jamais été exécuté, auquel cas algorithme.obtenir_parametres_match_api se replie
# automatiquement sur l'estimation pure pour toutes les équipes (comportement identique à avant
# ce correctif).
DATA_STATS_HISTORIQUES: Dict[str, Any] = {}

# CORRECTIF NOBELLIS (Bloc 1, classements réels, 11/09/2026) : classement officiel par équipe
# (position, points, buts marqués/encaissés réels de la saison en cours), synchronisé via
# synchroniser_classements_api.py - vide tant que ce script n'a jamais été exécuté, auquel cas
# algorithme.obtenir_parametres_match_api se replie automatiquement sur DATA_STATS_HISTORIQUES
# (historique CSV) puis sur l'estimation pure, exactement comme avant ce correctif.
DATA_CLASSEMENTS: Dict[str, Any] = {}

# AJOUT NOBELLIS (Bloc 2, résultats réels + forme, 12/09/2026) : forme récente réelle par équipe
# (Solution 6) - vide tant que synchroniser_resultats_api_football.py n'a jamais tourné, auquel
# cas algorithme.obtenir_parametres_match_api garde l'historique CSV (ou l'estimation) pour
# chaque équipe, exactement comme avant ce Bloc 2.
DATA_FORME_REELLE: Dict[str, Any] = {}

# AJOUT NOBELLIS (Chantier 3, head-to-head, 16/09/2026) : cache en mémoire des confrontations
# directes déjà synchronisées - jamais de lecture Firestore par affichage de match (voir
# head_to_head_api_football.obtenir_confrontations_en_cache, qui lit CE dict, jamais Firestore
# directement). Vide tant que la synchro nocturne n'a jamais tourné - le head-to-head n'est
# simplement pas affiché pour un match tant que sa paire n'est pas encore en cache.
DATA_H2H_CONFRONTATIONS: Dict[str, Any] = {}

# AJOUT NOBELLIS : connexion Firestore au démarrage. Remplace EN PLACE (jamais de réassignation)
# le contenu de DATA_PROFIL, DATA_ANALYSTES, DATA_PREDICTIONS_EXPERT et DATA_MATCHS si des
# données existent déjà sur Firestore (redémarrage d'une session précédente) ; sinon, pousse les
# données de démonstration actuelles comme état initial. Si Firestore est injoignable pour
# quelque raison que ce soit, cette fonction ne modifie STRICTEMENT rien et l'app démarre en
# mémoire pure - comportement identique à celui d'avant cet ajout.
firestore_client.charger_ou_initialiser(DATA_PROFIL, DATA_ANALYSTES, DATA_PREDICTIONS_EXPERT, DATA_MATCHS, DATA_STATS_HISTORIQUES, DATA_CLASSEMENTS)

# AJOUT NOBELLIS (Bloc 2, résultats réels + forme, 12/09/2026) : chargement direct, en lecture
# seule - jamais de valeur de démonstration écrite à sa place (même principe que
# stats_historiques), puisque synchroniser_resultats_api_football.py gère lui-même son
# initialisation. Même clé technique "_meta_derniere_maj_utc" que pour les classements
# (Solution 2), pour qu'algorithme.py applique la même règle de fraîcheur aux deux.
try:
    _doc_forme_reelle = firestore_client.lire_document(synchroniser_resultats_api_football.DOC_FORME_REELLE)
    if _doc_forme_reelle and isinstance(_doc_forme_reelle.get("items"), dict):
        DATA_FORME_REELLE.update(_doc_forme_reelle["items"])
        DATA_FORME_REELLE["_meta_derniere_maj_utc"] = _doc_forme_reelle.get("derniere_maj_utc")
        print(f"🔥 [NOBELLIS] Forme réelle chargée depuis Firestore ({len(_doc_forme_reelle['items'])} équipe(s)).", file=sys.stderr)
    else:
        print("🔥 [NOBELLIS] Aucune forme réelle synchronisée pour l'instant (voir synchroniser_resultats_api_football.py).", file=sys.stderr)
except Exception as _erreur_forme_reelle:
    print(f"⚠️  [NOBELLIS] Chargement de la forme réelle ignoré après erreur inattendue : {_erreur_forme_reelle}", file=sys.stderr)

# AJOUT NOBELLIS (Chantier 3, head-to-head, 16/09/2026) : même principe que la forme réelle -
# lecture seule, jamais de donnée de démonstration écrite à sa place.
try:
    _cache_h2h_initial = head_to_head_api_football.charger_cache_h2h_depuis_firestore()
    DATA_H2H_CONFRONTATIONS.update(_cache_h2h_initial)
    print(f"🔥 [NOBELLIS] Confrontations directes chargées depuis Firestore ({len(_cache_h2h_initial)} paire(s)).", file=sys.stderr)
except Exception as _erreur_h2h:
    print(f"⚠️  [NOBELLIS] Chargement des confrontations directes ignoré après erreur inattendue : {_erreur_h2h}", file=sys.stderr)

# AJOUT NOBELLIS (comptes séparés, Lot 1, 16/09/2026) : même principe - lecture seule au
# démarrage, jamais de donnée de démonstration écrite à sa place. DATA_PROFILS reste vide tant
# qu'aucun visiteur n'a encore été créé via obtenir_profil_courant() ni migré manuellement (voir
# migrer_profil_existant.py) - normal et attendu, pas une erreur.
try:
    _profils_initiaux = charger_profils_visiteurs_depuis_firestore()
    DATA_PROFILS.update(_profils_initiaux)
    print(f"🔥 [NOBELLIS] Profils visiteurs chargés depuis Firestore ({len(_profils_initiaux)} profil(s)).", file=sys.stderr)
except Exception as _erreur_profils:
    print(f"⚠️  [NOBELLIS] Chargement des profils visiteurs ignoré après erreur inattendue : {_erreur_profils}", file=sys.stderr)

# AJOUT NOBELLIS (automatisation quotidienne, 10/09/2026) : démarre le thread démon UNE SEULE
# fois, au chargement du module - voir _boucle_synchronisation_api_quotidienne ci-dessus pour le
# détail complet. daemon=True : ce thread ne bloque jamais l'arrêt du serveur, exactement comme
# le thread de synchronisation Firestore existant plus haut dans ce fichier.
threading.Thread(target=_boucle_synchronisation_api_quotidienne, daemon=True).start()

# CORRECTIF NOBELLIS (audit, détection automatique du thème système) : si le profil rechargé
# depuis Firestore n'a pas ce champ (données antérieures à ce correctif), il est traité comme
# "déjà choisi manuellement" - jamais de changement d'apparence surprise pour un utilisateur
# déjà actif. Seul un profil réellement neuf (voir sa valeur initiale plus haut) garde False.
DATA_PROFIL.setdefault("theme_defini_manuellement", True)

# CORRECTIF NOBELLIS (audit, faille n°8) : les matchs de démo rechargés depuis Firestore
# (redémarrage d'une session précédente) conservent l'heure de coup d'envoi de LEUR TOUT PREMIER
# calcul, qui devient obsolète avec le temps. Seuls les 4 matchs dont le rôle est d'être "à venir"
# pour alimenter les tests de publication (1, 2, 3, 6) sont concernés : si leur coup d'envoi est
# déjà dépassé, il est recalculé par rapport à MAINTENANT, avec le même écart relatif qu'à
# l'origine. Aucune règle anti-triche n'est modifiée : la vérification reste et restera toujours
# "coup d'envoi > horloge serveur actuelle" (voir match_encore_analysable_par_ia) - seule la
# DONNÉE de test se rafraîchit, jamais la RÈGLE. Les matchs 4, 5, 7 ne sont volontairement pas
# touchés ici : ils démontrent des états figés précis (résultat en attente, terminé, annulé) que
# ce rafraîchissement détruirait s'il les incluait.
#
# CORRECTIF NOBELLIS (audit, faille n°11) : la condition sur le champ "source" a été retirée -
# elle échouait silencieusement sur des matchs déjà sauvegardés dans Firestore AVANT l'introduction
# de ce champ (donnée absente, jamais égale à "demo"). Seul l'identifiant du match (1, 2, 3, 6) sert
# désormais de condition, une donnée qui existe depuis la toute première version du fichier - fiable
# quelle que soit l'ancienneté des données rechargées. Vérifié : les vrais matchs créés par
# l'admin/API reçoivent toujours un identifiant du type "m_xxxxxxxxxxxx" (voir
# api_admin_creer_match), jamais "1"/"2"/"3"/"6" - aucun risque de collision.
# Le statut, le score final et l'horodatage de clôture sont également réinitialisés ici : un match
# de test clôturé manuellement pendant une session de tests précédente resterait sinon bloqué sur
# "terminé" (voir match_encore_analysable_par_ia) même après le rafraîchissement de son heure -
# corriger l'heure sans corriger le statut aurait laissé le problème intact sous une autre forme.
_MAINTENANT_RAFRAICHISSEMENT_DEMO = datetime.now(timezone.utc)
_ECARTS_DEMO_A_VENIR = {"1": timedelta(hours=2), "2": timedelta(hours=5), "3": timedelta(days=1), "6": timedelta(days=3)}
for _match_demo in DATA_MATCHS:
    if _match_demo.get("id") not in _ECARTS_DEMO_A_VENIR:
        continue
    try:
        _kickoff_actuel = datetime.fromisoformat(str(_match_demo.get("kickoff_utc", "")))
    except (ValueError, TypeError):
        _kickoff_actuel = None
    if _kickoff_actuel is None or _kickoff_actuel <= _MAINTENANT_RAFRAICHISSEMENT_DEMO:
        _match_demo["kickoff_utc"] = (_MAINTENANT_RAFRAICHISSEMENT_DEMO + _ECARTS_DEMO_A_VENIR[_match_demo["id"]]).isoformat()
        _match_demo["heure"] = (_MAINTENANT_RAFRAICHISSEMENT_DEMO + _ECARTS_DEMO_A_VENIR[_match_demo["id"]]).strftime("%H:%M")
        _match_demo["statut"] = "a_venir"
        _match_demo["score_final"] = None
        _match_demo["cloture_timestamp"] = None


def _construire_match_info_pour_moteur(match: Dict[str, Any]) -> Dict[str, Any]:
    """
    CORRECTIF NOBELLIS : Couche adaptateur - construit les paramètres attendus par le moteur.
    CORRECTIF NOBELLIS (audit, import historique) : bascule effectuée, comme annoncé depuis
    l'origine de ce commentaire - algorithme.obtenir_parametres_match_api(...) remplace
    l'ancien obtenir_parametres_match_estime(...). Cette nouvelle fonction n'utilise de vraies
    données que pour les deux équipes SIMULTANÉMENT disponibles dans DATA_STATS_HISTORIQUES
    (importées une fois via importer_historique.py) ; sinon elle se replie intégralement sur
    l'estimation - la même structure de sortie qu'avant, rien d'autre dans cette fonction ni
    CORRECTIF NOBELLIS (Bloc 1, classements réels, 11/09/2026) : DATA_CLASSEMENTS (vrai
    classement officiel, synchroniser_classements_api.py) est maintenant prioritaire quand les
    deux équipes y figurent ; DATA_STATS_HISTORIQUES (CSV) reste le repli intermédiaire, puis
    l'estimation pure en dernier recours - aucun changement de comportement pour un match dont
    les équipes ne sont dans AUCUNE des deux sources.

    CORRECTIF NOBELLIS (Bloc 2, forme réelle, 12/09/2026) : DATA_FORME_REELLE (Solution 6 -
    transition automatique CSV -> forme réelle dès 5 vrais matchs accumulés) ajouté en dernier
    paramètre - aucun changement de comportement pour une équipe qui n'y figure pas encore.

    AJOUT NOBELLIS (Chantier 3, calcul, 16/09/2026) : confrontations_directes lu depuis
    DATA_H2H_CONFRONTATIONS (cache en mémoire, jamais Firestore ici - voir
    head_to_head_api_football.obtenir_confrontations_en_cache) - None si cette paire n'a jamais
    été synchronisée, aucun changement de comportement dans ce cas (repli automatique sur les
    facteurs existants, voir _score_confrontations_directes dans algorithme.py).
    """
    confrontations = head_to_head_api_football.obtenir_confrontations_en_cache(
        match["home"], match["away"], DATA_H2H_CONFRONTATIONS
    )
    parametres = algorithme.obtenir_parametres_match_api(
        match["home"], match["away"], match["id"], DATA_STATS_HISTORIQUES, DATA_CLASSEMENTS, DATA_FORME_REELLE, confrontations
    )
    match_info: Dict[str, Any] = {"home": match["home"], "away": match["away"]}
    match_info.update(parametres)
    return match_info


def generer_analyse_ia(match_id: str) -> Dict[str, Any]:
    """
    CORRECTIF NOBELLIS : Adaptateur vers le vrai moteur d'analyse (algorithme.py,
    10 000 simulations Monte Carlo), auparavant jamais branché. Traduit strictement le résultat
    du moteur vers les champs attendus par le template (home_xg, away_xg, classification,
    score_exact_flash, penalty_flash, etc.) - aucun champ manquant, aucun renommage oublié.

    Repli de sécurité : si le moteur échoue pour une raison imprévue (paramètre invalide,
    etc.), une analyse minimale mais cohérente est renvoyée plutôt que de faire planter la page.
    """
    match = next((m for m in DATA_MATCHS if m["id"] == str(match_id)), None)
    if not match:
        match = DATA_MATCHS[0]

    try:
        match_info = _construire_match_info_pour_moteur(match)
        # CORRECTIF NOBELLIS : seed dérivé du match_id (unique), pas seulement des noms d'équipes -
        # sans ça, deux matchs différents entre les mêmes équipes (aller/retour, saisons différentes)
        # partageraient la même empreinte de simulation Monte Carlo malgré des paramètres différents.
        seed_match = int(hashlib.sha256(f"{match['id']}|{match['home']}|{match['away']}".encode("utf-8")).hexdigest(), 16) % (2**32)
        resultat = algorithme.executer_analyse_moteur(match_info, _POIDS_IA_ACTUELS, seed=seed_match)

        meilleur_score = resultat["top_scores"][0]["score"] if resultat.get("top_scores") else "N/A"
        total_xg = resultat["xg_h"] + resultat["xg_a"]
        prefixe_piege = "⚠️ MATCH PIÈGE DÉTECTÉ : " if resultat.get("is_piege") else ""

        return {
            "match": match,
            "classification": f"{prefixe_piege}{resultat['identite']}",
            "home_xg": resultat["xg_h"],
            "away_xg": resultat["xg_a"],
            "p_home": resultat["p_home"],
            "p_nul": resultat["p_nul"],
            "p_away": resultat["p_away"],
            "incertitude": resultat["incertitude"],
            "scenario": resultat["scenario"],
            "vainqueur_flash": resultat["vainqueur_flash"],
            "buts_flash": resultat["buts_flash"],
            "btts_flash": resultat["btts_flash"],
            "score_exact_flash": meilleur_score,
            "penalty_flash": "Oui" if total_xg > 3.0 else "Non",
            "corners_flash": str(resultat["corners_flash"]),
            "cartons_flash": str(resultat["cartons_flash"]),
            "top_scores": resultat["top_scores"],
            "donnees_reelles": False  # CORRECTIF NOBELLIS : transparence - estimation tant que l'API n'est pas branchée
        }
    except Exception as erreur:
        print(f"🚨 [NOBELLIS IA] Erreur moteur d'analyse pour le match {match_id} : {erreur}", file=sys.stderr)
        p1, p2 = match["home"], match["away"]
        return {
            "match": match,
            "classification": "📊 ANALYSE INDISPONIBLE - DONNÉES INSUFFISANTES",
            "home_xg": 1.0, "away_xg": 1.0,
            "p_home": 33, "p_nul": 34, "p_away": 33,
            "incertitude": 90,
            "scenario": "Le décryptage tactique n'a pas pu être généré pour cette confrontation. Réessayez dans quelques instants.",
            "vainqueur_flash": "Indéterminé",
            "buts_flash": "Indéterminé",
            "btts_flash": "Indéterminé",
            "score_exact_flash": "N/A",
            "penalty_flash": "Indéterminé",
            "corners_flash": "N/A",
            "cartons_flash": "N/A",
            "top_scores": [],
            "donnees_reelles": False
        }
# =========================================================================
# ROUTAGE PRINCIPAL ET MOTEURS DE RENDU DE L'INTERFACE GRAPHIQUE (JINJA2)
# =========================================================================
@app.route('/')
def route_accueil():
    """
    Rendu de l'onglet d'accueil maîtresse. 
    Injecte la liste complète des matchs actifs avec leurs slugs et synchronise le profil.
    """
    # CORRECTIF NOBELLIS : un match clôturé disparaît de l'Accueil 5 minutes après sa clôture -
    # évite l'accumulation indéfinie de matchs joués qui saturerait l'interface avec le temps.
    # Copie superficielle pour ne JAMAIS muter DATA_MATCHS lui-même - l'historique complet reste
    # intact en mémoire, seule cette liste d'affichage est réduite.
    matchs_pour_affichage = []
    for match in obtenir_matchs_visibles():
        if not match_visible_sur_accueil(match):
            continue
        copie = dict(match)
        # CORRECTIF NOBELLIS : trois états distincts, pas deux - un match dont le coup d'envoi
        # est passé mais que l'admin n'a pas encore clôturé n'est ni "à venir" (le match se joue
        # ou est déjà fini dans la réalité) ni "clôturé" (aucun score connu à afficher).
        # Le confondre avec l'un ou l'autre induirait l'utilisateur en erreur.
        if copie.get("statut") == "termine":
            copie["etat_affichage"] = "termine"
        elif copie.get("statut") == "annule":
            copie["etat_affichage"] = "annule"
        elif not match_encore_analysable_par_ia(copie):
            copie["etat_affichage"] = "en_attente_cloture"
        else:
            copie["etat_affichage"] = "a_venir"
        matchs_pour_affichage.append(copie)

    # CORRECTIF NOBELLIS (audit, tri chronologique demandé) : les matchs d'aujourd'hui doivent
    # apparaître avant ceux de demain, triés par heure - pas dans l'ordre où Firestore les renvoie
    # (qui dépend de l'ordre d'insertion, pas du coup d'envoi). Fonction partagée avec
    # matchs_publiables (route_analyse) - voir _cle_tri_par_coup_denvoi, définie une seule fois.
    matchs_pour_affichage.sort(key=_cle_tri_par_coup_denvoi)

    reponse = make_response(render_template(
        'app_nobellis.html',
        active_tab='home',
        matchs=matchs_pour_affichage,
        profil=obtenir_profil_courant()  # CORRECTIF NOBELLIS (comptes séparés, Lot 2/1, 16/09/2026) : profil du visiteur courant, jamais plus DATA_PROFIL (singulier, partagé) - chaque visiteur voit désormais SON propre profil.
    ))
    # CORRECTIF NOBELLIS : cette page embarque le HTML + JS complet de l'application - un cache
    # navigateur qui la retient périmée fait tourner une ancienne version du code silencieusement,
    # sans aucune erreur visible. no-store force une requête fraîche systématique.
    reponse.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    reponse.headers["Pragma"] = "no-cache"
    return reponse

@app.route('/analyser/<match_id>')
@app.route('/onglet/analysis')
def route_analyse(match_id: Optional[str] = None):
    """
    Rendu de l'onglet de décryptage analytique de l'IA (Correction Alignement URL).
    Charge le premier match encore analysable par défaut si l'utilisateur accède directement
    par la navbar, sans match précis demandé.
    """
    erreur_blocage = None

    if not match_id:
        # CORRECTIF NOBELLIS : le choix par défaut doit respecter la MÊME règle que le blocage
        # lui-même (statut + coup d'envoi non passé) - sinon un visiteur sans match précisé
        # tomberait par hasard sur le message de blocage dès sa première visite, sans avoir
        # rien demandé de spécifique. Message dédié si aucun match ne correspond du tout.
        #
        # CORRECTIF NOBELLIS (audit, cohérence 72h dans toute l'application, 10/09/2026) :
        # auparavant next(...) prenait le PREMIER match trouvé dans l'ordre de stockage Firestore,
        # sans aucune limite de temps - avec les 12 compétitions, un visiteur ouvrant cet onglet
        # sans préciser de match pouvait tomber sur un match programmé dans plusieurs mois plutôt
        # que le plus proche. min(..., default=None) reprend exactement la même fenêtre de 72h
        # (match_visible_sur_accueil) et le même tri par proximité (_cle_tri_par_coup_denvoi) déjà
        # testés sur l'Accueil et sur "Publier une analyse" - jamais une quatrième logique
        # divergente pour la même règle.
        match_par_defaut = min(
            (m for m in obtenir_matchs_visibles() if match_encore_analysable_par_ia(m) and match_visible_sur_accueil(m)),
            key=_cle_tri_par_coup_denvoi,
            default=None,
        )
        if match_par_defaut:
            match_id = match_par_defaut["id"]
        else:
            erreur_blocage = "Aucun match à venir n'est actuellement disponible pour analyse."

    resultats_ia = {}
    if match_id and not erreur_blocage:
        match_demande = next((m for m in DATA_MATCHS if m["id"] == str(match_id)), None)
        # CORRECTIF NOBELLIS : le moteur IA ne doit plus jamais générer de simulation pour un
        # match dont le coup d'envoi réel est déjà passé - que l'admin ait eu le temps de le
        # clôturer officiellement ou non. Ce contrôle vit ICI, dans la route utilisateur, PAS à
        # l'intérieur de generer_analyse_ia() elle-même : cette fonction sert aussi de filet de
        # secours interne (fiches sans score explicite) qui doit continuer à fonctionner sans
        # interférence, peu importe le statut réel du match concerné.
        if match_demande and not match_encore_analysable_par_ia(match_demande):
            erreur_blocage = "Ce match a déjà commencé, aucune analyse prédictive n'est disponible."
        else:
            resultats_ia = generer_analyse_ia(match_id)

    reponse = make_response(render_template(
        'app_nobellis.html',
        active_tab='analysis',
        resultats=resultats_ia,
        erreur_analyse=erreur_blocage,
        profil=obtenir_profil_courant()  # CORRECTIF NOBELLIS (comptes séparés, Lot 2/1, 16/09/2026).
    ))
    # CORRECTIF NOBELLIS : voir route_accueil() - même protection anti-cache, même raison.
    reponse.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    reponse.headers["Pragma"] = "no-cache"
    return reponse
SEUIL_JOURS_MINIMUM_PRESENCE = 14  # CORRECTIF NOBELLIS : cohérence avec le seuil de volume Wilson -
                                     # aucun pourcentage affiché sur un échantillon trop jeune pour être fiable.


def calculer_presence_analyste(analyste: Dict[str, Any]) -> Dict[str, Any]:
    """
    CORRECTIF NOBELLIS : Transparence publique du cycle de statut - mesure la DURÉE réelle
    d'absence cumulée, pas un simple comptage de réactivations (contournable en restant sous
    un seuil de comptage peu importe la durée de chaque absence). Le pourcentage de présence
    est impossible à manipuler en fractionnant ou en évitant un nombre rond d'événements.
    N'affiche rien avant 14 jours d'ancienneté (même standard de rigueur que le score de Wilson
    ailleurs dans l'app - un échantillon trop jeune ne prouve rien).
    """
    date_activation_str = analyste.get("date_activation_initiale")
    if not date_activation_str:
        return {"jours_membre": None, "pourcentage_presence": None}

    date_activation = datetime.fromisoformat(date_activation_str)
    jours_membre = (datetime.now(timezone.utc) - date_activation).total_seconds() / 86400

    if jours_membre < SEUIL_JOURS_MINIMUM_PRESENCE:
        return {"jours_membre": round(jours_membre), "pourcentage_presence": None}

    secondes_inactif = analyste.get("total_secondes_inactif", 0)
    jours_actifs = max(0.0, jours_membre - (secondes_inactif / 86400))
    pourcentage = max(0, min(100, round((jours_actifs / jours_membre) * 100)))
    return {"jours_membre": round(jours_membre), "pourcentage_presence": pourcentage}


@app.route('/onglet/analysts')
def route_analystes():
    """
    Rendu de l'onglet de la communauté et du réseau d'experts certifiés (Correction Alignement URL).
    Exporte la grille des analystes avec leurs indicateurs de suivi unifiés.

    CORRECTIF NOBELLIS : Classement automatique et mérité. Les analystes sont désormais
    triés par score de Wilson décroissant (précision réelle pondérée par le volume),
    plutôt que par ordre d'insertion en mémoire. Un analyste ne peut plus "monter" dans
    le classement autrement qu'en performant réellement - le tri est recalculé à chaque
    affichage, jamais figé.
    """
    # CORRECTIF NOBELLIS : seuls les analystes au statut "actif" apparaissent dans le classement
    # public - un profil désactivé (voir /api/profil/quitter-analyste) disparaît de la vue
    # publique sans jamais perdre son historique interne (recalculé normalement en arrière-plan).
    # CORRECTIF NOBELLIS (audit, faille n°9) : un analyste qui n'a jamais publié la moindre analyse
    # n'apparaît pas non plus - filtré ici, en amont de tout calcul de mérite ou de classement,
    # pour qu'un profil vide ne puisse jamais fausser le podium ou le tri par mérite.
    # CORRECTIF NOBELLIS (24/09/2026, décision de l'utilisateur, remplace l'ancienne règle "il ne
    # redisparaît jamais") : un analyste dont TOUTES les fiches ont expiré n'est plus affiché ici
    # non plus, et réapparaît dès sa prochaine publication. Seul l'AFFICHAGE change : ses
    # statistiques, sa précision, son score de mérite, son historique et ses abonnés restent
    # intacts - sa position se recalcule par les mêmes formules qu'avant dès qu'il revient.
    analystes_actifs = [
        a for a in DATA_ANALYSTES
        if a.get("statut_compte", "actif") == "actif" and a.get("liste_pronostics")
        and any(fiche_est_visible(fiche.get("match_id"), fiche.get("publie_le")) for fiche in a["liste_pronostics"])
    ]

    # CORRECTIF NOBELLIS : calcul de la présence réelle attaché à chaque analyste affiché,
    # recalculé à chaque chargement de page - jamais figé, jamais falsifiable côté client.
    # CORRECTIF NOBELLIS (audit, nouveau système de classement) : série de victoires EN COURS
    # calculée ici aussi, pour chaque analyste affiché - nécessaire à calculer_score_classement_nobellis
    # juste en dessous. Stockée sous une clé préfixée par "_" pour signaler clairement qu'il s'agit
    # d'une donnée dérivée à l'affichage, jamais persistée telle quelle dans DATA_ANALYSTES.
    for analyste in analystes_actifs:
        analyste.update(calculer_presence_analyste(analyste))
        analyste["_serie_courante_calculee"] = calculer_serie_et_forme_recente(analyste["id"])["serie_courante"]

    # CORRECTIF NOBELLIS : classement en 3 blocs, décidé avec l'utilisateur -
    # 1) Top 3 mérite pur, fixe, identique pour tout le monde - pousse à la qualité réelle,
    #    jamais contourné par un statut d'abonnement.
    # 2) Abonnements de l'utilisateur courant, ET sa propre fiche s'il est lui-même analyste
    #    (hors ceux déjà dans le Top 3, pour ne jamais afficher deux fois la même carte),
    #    triés par la MÊME formule de mérite que le Top 3 - pas par date de publication, pour
    #    rester cohérent avec l'objectif d'encourager la qualité plutôt que la simple fréquence
    #    de publication. Départage par total_posts (nombre réel d'analyses publiées) entre
    #    profils encore à 0% faute d'historique résolu - un profil qui a déjà publié plusieurs
    #    fiches démontre un engagement réel supérieur à un profil resté inactif, sans inventer
    #    de nouveau critère non suivi par l'app.
    # 3) Le reste, dans le même ordre de mérite qu'avant ce correctif.
    # CORRECTIF NOBELLIS (audit, nouveau système de classement) : formule_merite fixe remplacée par
    # calculer_score_classement_nobellis - score continu (fiabilité Wilson + série en cours +
    # coefficient d'activité qui décroît en douceur). Aucune position n'est jamais acquise : un
    # analyste plus méritant reprend automatiquement sa place à chaque recalcul, et un analyste
    # inactif redescend progressivement, jour après jour, jamais d'un coup. Départage par
    # pronostics_resolus puis total_posts, inchangé, pour les cas d'égalité stricte du score continu.
    formule_merite = lambda a: (calculer_score_classement_nobellis(a), a.get("pronostics_resolus", 0), a.get("total_posts", 0))

    classement_merite = sorted(analystes_actifs, key=formule_merite, reverse=True)

    top3 = classement_merite[:3]
    ids_top3 = {a["id"] for a in top3}

    # CORRECTIF NOBELLIS (comptes séparés, Lot 2/1, 16/09/2026) : profil du visiteur courant,
    # jamais plus DATA_PROFIL (singulier, partagé) - récupéré une seule fois, réutilisé plus bas
    # (id_analyste_courant, fiches_deja_vues, et le rendu final) pour ne jamais créer le profil
    # plusieurs fois par erreur dans une même requête.
    profil_visiteur = obtenir_profil_courant()
    id_analyste_courant = profil_visiteur.get("analyst_id")
    fiches_deja_vues = set(profil_visiteur.get("fiches_deja_vues", []))

    # CORRECTIF NOBELLIS (audit, mise en avant "non-vu") : retrouve, pour un analyste donné, la
    # date de publication de sa fiche non-vue la plus récente - None si aucune de ses fiches n'a
    # de publie_le (fiches historiques hors système), ou si elles ont toutes déjà été consultées.
    def _derniere_publication_non_vue(analyste: Dict[str, Any]) -> Optional[datetime]:
        dates_non_vues = []
        for fiche in analyste.get("liste_pronostics") or []:
            if fiche.get("id") in fiches_deja_vues or not fiche.get("publie_le"):
                continue
            try:
                dates_non_vues.append(datetime.fromisoformat(str(fiche["publie_le"])))
            except (ValueError, TypeError):
                continue
        return max(dates_non_vues) if dates_non_vues else None

    # CORRECTIF NOBELLIS (audit, mise en avant "non-vu") : priorité à toute publication récente
    # jamais consultée par ce visiteur - la plus récente non-vue en tête. Sans rien de non-vu,
    # retour immédiat au tri de mérite habituel (formule_merite, inchangée). Cette mise en avant
    # est temporaire et s'efface d'elle-même dès l'ouverture réelle de la fiche (voir
    # api_acceder_fiche_pronostic) - jamais un minuteur, uniquement une vraie consultation.
    def _cle_tri_abonnements(analyste: Dict[str, Any]):
        derniere_non_vue = _derniere_publication_non_vue(analyste)
        if derniere_non_vue is not None:
            return (1, derniere_non_vue)
        return (0, formule_merite(analyste))

    abonnements = sorted(
        [
            a for a in analystes_actifs
            # CORRECTIF NOBELLIS (comptes séparés, Lot 3, 16/09/2026) : "suivi" vérifié dans la
            # liste PROPRE au visiteur courant (déjà récupéré plus haut dans cette même fonction,
            # voir profil_visiteur) - jamais plus a.get("is_following"), l'ancien champ partagé
            # entre tous les visiteurs (faille de conception corrigée aujourd'hui).
            if (a["id"] in profil_visiteur.get("analystes_suivis", []) or a["id"] == id_analyste_courant) and a["id"] not in ids_top3
        ],
        key=_cle_tri_abonnements,
        reverse=True
    )
    ids_abonnements = {a["id"] for a in abonnements}

    reste = [a for a in classement_merite if a["id"] not in ids_top3 and a["id"] not in ids_abonnements]

    analystes_classes = top3 + abonnements + reste

    # CORRECTIF NOBELLIS : filtrage d'affichage uniquement - copie superficielle de chaque
    # analyste pour ne JAMAIS muter DATA_ANALYSTES lui-même (liste_pronostics reste intacte pour
    # le calcul des statistiques ailleurs, seule cette copie destinée au template est réduite).
    analystes_pour_affichage = []
    analystes_suivis_par_ce_visiteur = set(profil_visiteur.get("analystes_suivis", []))
    for analyste in analystes_classes:
        copie_affichage = dict(analyste)
        copie_affichage["liste_pronostics"] = [
            fiche for fiche in analyste.get("liste_pronostics", [])
            if fiche_est_visible(fiche.get("match_id"), fiche.get("publie_le"))
        ]
        # CORRECTIF NOBELLIS (comptes séparés, Lot 3, 16/09/2026) : is_following recalculé ICI,
        # pour CE visiteur précis - jamais depuis un éventuel champ is_following déjà présent sur
        # l'analyste lui-même (resté en mémoire comme donnée de démonstration historique, jamais
        # lu ni écrit par aucune route depuis ce correctif - voir api_suivre_analyste_toggle).
        # copie_affichage["is_following"] écrase ici toute ancienne valeur héritée du dict(analyste)
        # ci-dessus, avec la vraie valeur, personnelle à ce visiteur.
        copie_affichage["is_following"] = analyste["id"] in analystes_suivis_par_ce_visiteur
        analystes_pour_affichage.append(copie_affichage)

    # CORRECTIF NOBELLIS : la zone "publier une analyse" ne doit proposer QUE des matchs
    # encore réellement publiables - même règle que match_encore_analysable_par_ia (statut +
    # coup d'envoi non passé), réutilisée telle quelle pour ne pas dupliquer cette logique de
    # sécurité une troisième fois dans le fichier. Sans ça, l'interface proposerait des matchs
    # que la validation finale rejette de toute façon.
    #
    # CORRECTIF NOBELLIS (audit, bug signalé 10/09/2026) : match_encore_analysable_par_ia() n'a
    # AUCUNE borne supérieure - un match dans 8 mois passait ce test aussi bien qu'un match dans
    # 1h. Avec les 12 compétitions et leurs milliers de matchs programmés sur la saison entière,
    # cette zone tentait de générer un bloc DOM (2 logos + un accordéon) par match AUTORISÉ, sans
    # AUCUNE limite - des milliers d'éléments, chacun avec 2 requêtes d'image, expliquant la
    # lenteur signalée à l'ouverture de cet onglet. Correctif : réutilise match_visible_sur_accueil
    # (déjà testé, déjà validé), qui applique la même fenêtre de 72h que l'Accueil - jamais une
    # deuxième logique de fenêtre divergente. Tri chronologique ajouté pour la même raison
    # d'ergonomie que sur l'Accueil : le prochain match à analyser doit apparaître en premier.
    matchs_publiables = sorted(
        (m for m in obtenir_matchs_visibles() if match_encore_analysable_par_ia(m) and match_visible_sur_accueil(m)),
        key=_cle_tri_par_coup_denvoi,
    )

    matchs_deja_publies = calculer_matchs_deja_publies()

    reponse = make_response(render_template(
        'app_nobellis.html',
        active_tab='analysts',
        analystes=analystes_pour_affichage,
        matchs=obtenir_matchs_visibles(),
        matchs_publiables=matchs_publiables,
        matchs_deja_publies=matchs_deja_publies,
        profil=profil_visiteur  # CORRECTIF NOBELLIS (comptes séparés, Lot 2/1, 16/09/2026) : réutilise la variable déjà obtenue plus haut, jamais un second appel/objet différent dans la même requête.
    ))
    # CORRECTIF NOBELLIS : voir route_accueil() - même protection anti-cache, même raison.
    reponse.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    reponse.headers["Pragma"] = "no-cache"
    return reponse

@app.route('/api/publier/formulaire/<match_id>')
def api_formulaire_publication_match(match_id: str):
    """
    CORRECTIF NOBELLIS (audit, chargement à la demande) : sert le formulaire complet d'UN SEUL
    match, uniquement quand l'analyste ouvre réellement ce match précis dans l'onglet Analystes -
    au lieu d'envoyer les ~30 champs de TOUS les matchs publiables d'un coup (plus d'1 Mo pour
    une page que la majorité des visiteurs ne fait qu'entrouvrir sur un seul match, voire aucun).

    Sécurité : mêmes vérifications que matchs_publiables dans route_analystes (le match doit
    exister ET être encore analysable - coup d'envoi non passé) - jamais une règle différente ou
    plus permissive juste parce que c'est un chemin d'accès différent. Réponse HTML brute (un
    fragment, pas une page complète), à insérer directement dans la page par le front-end.
    """
    if not verifier_rate_limit(request.remote_addr, max_requetes=15):
        return "<div style='padding:20px;color:var(--text-muted);text-align:center;'>Trop de requêtes. Réessayez dans un instant.</div>", 429

    match = next((m for m in obtenir_matchs_visibles() if m.get("id") == str(match_id)), None)
    if not match or not match_encore_analysable_par_ia(match):
        return (
            "<div style='padding:20px;color:var(--text-muted);text-align:center;'>"
            "Ce match n'est plus disponible pour la publication (déjà commencé ou introuvable)."
            "</div>",
            404,
        )

    fragment = render_template(
        'fragment_formulaire_marche.html',
        match=match,
        matchs_deja_publies=calculer_matchs_deja_publies(),
    )
    reponse = make_response(fragment)
    reponse.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return reponse

@app.route('/onglet/profile')
def route_profil():
    """
    Rendu de l'onglet de configuration du profil et de gestion des préférences du système (Correction Alignement URL).

    CORRECTIF NOBELLIS : calcul de l'horodatage exact de fin de cooldown (48h après désactivation),
    transmis au template pour affichage d'un compte à rebours visible EN AMONT du clic - sans ça,
    un utilisateur qui a quitté ne voit rien lui indiquer qu'il doit attendre, et perçoit le refus
    au clic comme un bug plutôt qu'une règle claire. Recalculé à chaque chargement de page à partir
    de l'horodatage serveur réel (jamais périmé, jamais falsifiable côté client).
    """
    cooldown_fin_iso = None
    # CORRECTIF NOBELLIS (comptes séparés, Lot 2/1, 16/09/2026) : profil du visiteur courant,
    # jamais plus DATA_PROFIL (singulier, partagé) - récupéré une seule fois, réutilisé pour
    # toutes les lectures de cette route.
    profil_visiteur = obtenir_profil_courant()
    if not profil_visiteur.get("is_analyst"):
        profil_analyste_inactif = next((a for a in DATA_ANALYSTES if a["id"] == profil_visiteur.get("analyst_id")), None)
        if profil_analyste_inactif and profil_analyste_inactif.get("date_desactivation"):
            date_desactivation = datetime.fromisoformat(profil_analyste_inactif["date_desactivation"])
            date_fin_cooldown = date_desactivation + timedelta(hours=48)
            if date_fin_cooldown > datetime.now(timezone.utc):
                cooldown_fin_iso = date_fin_cooldown.isoformat()

    # CORRECTIF NOBELLIS : précision réelle / gagnées / perdues - ces chiffres étaient déjà
    # calculés avec rigueur (score de Wilson, exclusion des matchs annulés) pour la fiche
    # analyste publique, mais jamais reliés à la page Profil du propriétaire lui-même. On va
    # chercher SA PROPRE fiche analyste (via analyst_id) plutôt que d'inventer un second calcul
    # - une seule source de vérité pour ce nombre, jamais deux logiques divergentes.
    stats_analyste_proprietaire = next(
        (a for a in DATA_ANALYSTES if a["id"] == profil_visiteur.get("analyst_id")), None
    )
    if stats_analyste_proprietaire:
        resolus = stats_analyste_proprietaire.get("pronostics_resolus", 0)
        gagnes = stats_analyste_proprietaire.get("pronostics_gagnes", 0)
        stats_profil_proprietaire = {
            "precision": stats_analyste_proprietaire.get("precision", 0),
            "gagnes": gagnes,
            "perdus": max(0, resolus - gagnes),
        }
    else:
        # Aucun compte analyste jamais créé - repli honnête à zéro, jamais un chiffre inventé.
        stats_profil_proprietaire = {"precision": 0, "gagnes": 0, "perdus": 0}

    # CORRECTIF NOBELLIS : rang réel + progression réelle + série + forme récente - même source
    # de vérité que stats_analyste_proprietaire ci-dessus, jamais un second calcul divergent.
    stats_rang_proprietaire = calculer_progression_palier(stats_analyste_proprietaire)
    stats_serie_proprietaire = calculer_serie_et_forme_recente(profil_visiteur.get("analyst_id"))

    reponse = make_response(render_template(
        'app_nobellis.html',
        active_tab='profile',
        profil=profil_visiteur,
        cooldown_analyste_fin=cooldown_fin_iso,
        debug_diagnostic=DEBUG_DIAGNOSTIC_ACTIF,
        version_serveur=HEURE_DEMARRAGE_SERVEUR,
        stats_perso=stats_profil_proprietaire,
        stats_rang=stats_rang_proprietaire,
        stats_serie=stats_serie_proprietaire
    ))
    # CORRECTIF NOBELLIS : voir route_accueil() - même protection anti-cache, même raison.
    reponse.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    reponse.headers["Pragma"] = "no-cache"
    return reponse
# =========================================================================
# TUNNELS D'API REST ASYNCHRONES : ACCÈS ET DÉCRYPTAGE DE FICHES TACTIQUES
# =========================================================================
@app.route('/api/pronostics/acceder/<prono_id>', methods=['POST', 'GET'])
def api_acceder_fiche_pronostic(prono_id: str):
    """
    Déverrouillage asynchrone sécurisé de la fiche technique d'un analyste.
    Forçage des en-têtes anti-cache pour détruire la persistance du DOM sur Android.
    """
    if not verifier_rate_limit(request.remote_addr, max_requetes=15):
        return jsonify({"succes": False, "erreur": "Saturation de flux. Ralentissez vos requêtes."}), 429

    prediction_data = DATA_PREDICTIONS_EXPERT.get(prono_id)
    if not prediction_data:
        return jsonify({"succes": False, "erreur": "Cette fiche technique n'a pas pu être déchiffrée."}), 404

    # CORRECTIF NOBELLIS : même règle de visibilité que la liste des cartes analystes - un accès
    # direct par identifiant ne doit jamais contourner la fenêtre de 30 minutes après clôture.
    if not fiche_est_visible(prediction_data.get("match_id"), prediction_data.get("publie_le")):
        return jsonify({"succes": False, "erreur": "Analyse clôturée, match terminé."}), 403

    # Extraction de la matrice interne des sous-marchés
    predictions = prediction_data.get("predictions", {})
    
    # Normalisation algorithmique du choix 1N2
    brut_1n2 = str(predictions.get('market_1n2', 'N/A')).upper()
    choix_1n2 = "1" if "DOMICILE" in brut_1n2 or "V1" in brut_1n2 or "1" in brut_1n2 else ("2" if "EXTÉRIEUR" in brut_1n2 or "V2" in brut_1n2 or "2" in brut_1n2 else ("X" if "NUL" in brut_1n2 or "X" in brut_1n2 else brut_1n2))
    
    # CORRECTIF DE COUPURE : Le score saisi par l'analyste est désormais TOUJOURS prioritaire
    # (le champ market_score est correctement transmis par le formulaire depuis la mise à jour du template).
    # Le repli sur l'IA ne sert plus que de filet de sécurité pour les anciennes fiches sans score renseigné.
    score_exact = str(predictions.get('market_score', '')).strip()
    if not score_exact:
        match_id = prediction_data.get("match_id", "1")
        # CORRECTIF NOBELLIS (audit, faille n°13) : ce repli appelait le moteur IA sans jamais
        # vérifier si le match avait déjà commencé - un contournement réel de la règle qui bloque
        # pourtant explicitement toute génération après le coup d'envoi côté onglet Analyse (voir
        # route_analyse). Même fonction de garde utilisée ici, pour ne jamais avoir deux définitions
        # différentes de "match encore analysable". Sécurité par défaut : match introuvable ->
        # traité comme non analysable, jamais l'inverse.
        match_pour_verification = next((m for m in DATA_MATCHS if m.get("id") == str(match_id)), None)
        if match_pour_verification and match_encore_analysable_par_ia(match_pour_verification):
            analyse_ia = generer_analyse_ia(match_id)
            score_exact = analyse_ia.get("score_exact_flash", "Calcul en cours")
        else:
            score_exact = "Non communiqué par l'analyste"
        
    conseil_principal = f"{choix_1n2} & Score : {score_exact}"
    
    # CORRECTIF FONDAMENTAL D'ARGUMENTATION : Mappage étanche avec data.argument du JavaScript
    argumentation = prediction_data.get("comment_analyst", "").strip()
    if not argumentation or argumentation == "Aucune argumentation rédigée.":
        argumentation = "Décryptage Tactique Nobellis : Configuration de confrontation standard. Évaluation des volumes de buts et d'intensité physique en cours."

    # CORRECTIF NOBELLIS : Intégration du marché "Cartons Rouges" (précédemment absent de la réponse API)
    cartons_rouges = str(predictions.get('market_rouges', '')).strip()
    if not cartons_rouges:
        cartons_rouges = "Non communiqué"

    # CORRECTIF NOBELLIS : Nom du match et consensus, désormais stockés à la source (fiche statique ou publication)
    match_nom = prediction_data.get("match_nom", "")
    consensus_pct = prediction_data.get("consensus_pct")

    # CORRECTIF NOBELLIS : Nom de l'analyste, retrouvé via l'identifiant déjà stocké dans la fiche
    expert_id = prediction_data.get("expert_id", "")
    expert_trouve = next((a for a in DATA_ANALYSTES if a["id"] == expert_id), None)
    expert_nom = expert_trouve["nom"] if expert_trouve else "Analyste Nobellis"

    # CORRECTIF NOBELLIS : Preuve de publication vérifiable - transmise UNIQUEMENT si elle existe
    # réellement (fiches passées par le nouveau système). Jamais de fausse preuve sur les fiches
    # historiques/démonstration comme p_101, qui n'ont jamais été soumises à ce contrôle.
    publie_le = prediction_data.get("publie_le")  # None si absent - le frontend doit alors ne rien afficher
    resultat = prediction_data.get("resultat", "en_attente")

    # CORRECTIF NOBELLIS (audit, mise en avant "non-vu") : cette fiche vient d'être réellement
    # consultée - on l'ajoute au carnet des fiches vues du visiteur, pour qu'elle perde sa mise en
    # avant temporaire dans le bloc "Abonnements" (voir route_analystes). Seul déclencheur possible :
    # une vraie ouverture via cette route, jamais une action indirecte ou automatique.
    # CORRECTIF NOBELLIS (comptes séparés, Lot 2/2, 16/09/2026) : profil du visiteur courant,
    # jamais plus DATA_PROFIL (singulier, partagé) - et sauvegarde via sauvegarder_profil_visiteur
    # (la vraie collection multi-visiteurs), jamais _synchroniser_firestore_arriere_plan qui
    # n'écrit que sur l'ANCIEN document "profil" unique, obsolète pour ce nouveau système.
    profil_visiteur = obtenir_profil_courant()
    fiches_vues = profil_visiteur.setdefault("fiches_deja_vues", [])
    if prono_id not in fiches_vues:
        fiches_vues.append(prono_id)
        sauvegarder_profil_visiteur(obtenir_visiteur_id(), profil_visiteur)

    # Création de la réponse JSON unifiée
    reponse = jsonify({
        "succes": True,
        "conseil": conseil_principal,
        "argument": argumentation,  # Aligné à 100% avec votre JavaScript front-end
        "cartons_rouges": cartons_rouges,  # Nouveau champ, aligné avec market_rouges du formulaire
        "match_nom": match_nom,
        "consensus_pct": consensus_pct,
        "expert_nom": expert_nom,
        "publie_le": publie_le,
        "resultat": resultat,
        "details": predictions
    })
    
    # VERROUILLAGE ANTI-CACHE HYDROFUGE DE L'INTERFACE UTILISATEUR MOBILE
    reponse.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    reponse.headers["Pragma"] = "no-cache"
    reponse.headers["Expires"] = "0"
    
    return reponse, 200

@app.route('/api/analystes/suivre/<analyste_id>', methods=['POST'])
def api_suivre_analyste_toggle(analyste_id: str):
    """
    Gestionnaire unifié de bascule (Toggle) Follow / Unfollow.

    CORRECTIF NOBELLIS (comptes séparés, Lot 3, 16/09/2026) : "suivre" est désormais une
    préférence PROPRE À CHAQUE VISITEUR (profil_visiteur["analystes_suivis"]), jamais un
    interrupteur partagé sur la fiche de l'analyste (expert["is_following"], l'ancien
    comportement - corrigé, plus jamais lu ni écrit ici). expert["parrainages"] reste, lui, un
    vrai compteur global légitime, ajusté seulement selon LE changement de CE visiteur précis
    (jamais un double comptage si le même visiteur clique deux fois de suite sans changement net).
    """
    if not verifier_rate_limit(request.remote_addr, max_requetes=10):
        return jsonify({"succes": False, "erreur": "Trop de requêtes."}), 429

    # CORRECTIF NOBELLIS (audit, interblocage évité, 16/09/2026) : obtenir_profil_courant() prend
    # déjà _VERROU_MUTATION_ETAT en interne - appelé ICI, avant le verrou ci-dessous, jamais dedans
    # (même leçon que api_reinitialiser_profil, voir son commentaire pour le détail du risque).
    profil_visiteur = obtenir_profil_courant()

    # CORRECTIF NOBELLIS : Anti-collusion - un analyste ne peut pas se suivre lui-même
    # pour gonfler artificiellement son propre nombre de followers/preuve sociale.
    if str(analyste_id) == str(profil_visiteur.get("analyst_id", "")):
        return jsonify({"succes": False, "erreur": "Vous ne pouvez pas vous suivre vous-même."}), 403

    expert = next((a for a in DATA_ANALYSTES if a["id"] == str(analyste_id)), None)
    if not expert:
        return jsonify({"succes": False, "erreur": "Analyste introuvable."}), 404

    with _VERROU_MUTATION_ETAT:
        analystes_suivis = profil_visiteur.setdefault("analystes_suivis", [])
        if str(analyste_id) not in analystes_suivis:
            analystes_suivis.append(str(analyste_id))
            expert["parrainages"] = expert.get("parrainages", 0) + 1
            is_following_now = True
        else:
            analystes_suivis.remove(str(analyste_id))
            expert["parrainages"] = max(0, expert.get("parrainages", 0) - 1)
            is_following_now = False

        sauvegarder_profil_visiteur(obtenir_visiteur_id(), profil_visiteur)
        # CORRECTIF NOBELLIS (audit) : synchronisation Firestore manquante identifiée et corrigée -
        # sans cet appel, le statut "suivi" et le compteur de parrainages restaient corrects en
        # mémoire mais n'étaient jamais persistés, contrairement à toutes les autres routes de
        # mutation. Un redémarrage du serveur aurait silencieusement perdu ce suivi.
        _synchroniser_firestore_arriere_plan(data_analystes=DATA_ANALYSTES)

    return jsonify({
        "succes": True,
        "is_following": is_following_now,
        "nouveau_total_followers": expert["parrainages"]
    }), 200
# =========================================================================
# CORRECTIF NOBELLIS : CERTIFICATION MÉRITÉE - SCORE DE WILSON (BORNE BASSE)
# =========================================================================
# Seuils calibrés provisoirement sur des standards du secteur du pronostic sportif.
# À RECALIBRER avec les données réelles de la plateforme une fois un historique suffisant
# disponible (voir échange avec l'équipe produit).
SEUIL_VOLUME_MINIMUM: int = 20          # Pronostics résolus minimum avant toute certification
SEUIL_WILSON_CERTIFIE: float = 0.50     # Borne basse de Wilson minimum pour "Certifié"
SEUIL_WILSON_ELITE: float = 0.62        # Borne basse de Wilson minimum pour "Élite"
Z_SCORE_CONFIANCE: float = 1.96         # Intervalle de confiance à 95%


def calculer_score_wilson(gagnes: int, resolus: int, z: float = Z_SCORE_CONFIANCE) -> float:
    """
    CORRECTIF NOBELLIS : Borne basse de l'intervalle de confiance de Wilson.
    Empêche qu'un petit échantillon chanceux (ex: 4 victoires sur 5) batte
    un gros échantillon régulier (ex: 60 victoires sur 100) dans le classement/la certification.
    Retourne 0.0 si aucun pronostic résolu (aucune preuve = aucun mérite).
    """
    if resolus <= 0:
        return 0.0
    p_observe = gagnes / resolus
    denominateur = 1.0 + (z * z) / resolus
    centre = p_observe + (z * z) / (2 * resolus)
    marge = z * math.sqrt((p_observe * (1 - p_observe) / resolus) + (z * z) / (4 * resolus * resolus))
    borne_basse = (centre - marge) / denominateur
    return max(0.0, round(borne_basse, 4))


def calculer_consensus_reel(match_id: str, choix_1n2: str) -> int:
    """
    CORRECTIF NOBELLIS : Consensus réel, calculé sur les pronostics 1N2 effectivement publiés
    par les analystes sur ce match - jamais un chiffre fixe. S'il s'agit du premier pronostic
    publié sur ce match, le consensus vaut honnêtement 100% (il n'y a encore rien à contredire),
    plutôt qu'un chiffre inventé.

    Limite connue et assumée : ce calcul est figé au moment de la publication. Si un autre
    analyste publie ensuite sur le même match, les fiches déjà publiées ne se mettent pas
    à jour rétroactivement - cette mise à jour dynamique fait partie du chantier base de données.
    """
    choix_normalise = str(choix_1n2).strip().upper()
    predictions_du_match = [
        p for p in DATA_PREDICTIONS_EXPERT.values()
        if str(p.get("match_id")) == str(match_id)
    ]
    if not predictions_du_match:
        return 100
    accords = sum(
        1 for p in predictions_du_match
        if str(p.get("predictions", {}).get("market_1n2", "")).strip().upper() == choix_normalise
    )
    return round(accords / len(predictions_du_match) * 100)


def resoudre_pronostics_du_match(match_id: str, score_final: str) -> None:
    """
    CORRECTIF NOBELLIS : Comparaison déterministe entre le pronostic 1X2 de chaque analyste
    et le score final officiel. Aucune appréciation manuelle - une règle fixe, identique pour tous.
    """
    try:
        buts_dom_str, buts_ext_str = score_final.split('-')
        buts_dom, buts_ext = int(buts_dom_str.strip()), int(buts_ext_str.strip())
    except (ValueError, AttributeError):
        return  # Format invalide - aucune résolution n'est effectuée, aucune donnée n'est corrompue.

    if buts_dom > buts_ext:
        resultat_reel = "V1"
    elif buts_dom < buts_ext:
        resultat_reel = "V2"
    else:
        resultat_reel = "X"

    for prono_id, prediction_data in DATA_PREDICTIONS_EXPERT.items():
        if str(prediction_data.get("match_id")) != str(match_id):
            continue
        choix_1n2 = str(prediction_data.get("predictions", {}).get("market_1n2", "")).strip().upper()
        if choix_1n2 in ("V1", "X", "V2"):
            resultat_calcule = "gagne" if choix_1n2 == resultat_reel else "perdu"
        else:
            resultat_calcule = "non_evalue"
        prediction_data["resultat"] = resultat_calcule

        # CORRECTIF NOBELLIS : Synchronisation du résultat vers la fiche affichée (liste_pronostics)
        # pour que le statut gagné/perdu/en attente soit directement lisible côté frontend.
        expert_id = prediction_data.get("expert_id")
        analyste_proprietaire = next((a for a in DATA_ANALYSTES if a["id"] == expert_id), None)
        if analyste_proprietaire:
            fiche_affichee = next((f for f in analyste_proprietaire.get("liste_pronostics", []) if f.get("id") == prono_id), None)
            if fiche_affichee:
                fiche_affichee["resultat"] = resultat_calcule

    recalculer_stats_tous_analystes()


DUREE_VISIBILITE_APRES_CLOTURE = timedelta(minutes=30)
DUREE_VISIBILITE_ACCUEIL_APRES_CLOTURE = timedelta(minutes=30)
# CORRECTIF NOBELLIS (audit, faille n°12) : durée standard estimée d'un match (mi-temps et
# arrêts de jeu compris), utilisée par l'Accueil tant qu'aucune vraie clôture n'a eu lieu (voir
# match_visible_sur_accueil plus bas) ET, depuis le 24/09/2026, par la durée de vie des analyses
# publiées (voir fiche_est_visible / _visible_jusqu_a_fin_de_match_plus).
# N'affecte pas match_encore_analysable_par_ia, ni aucune règle anti-triche.
# CORRECTIF NOBELLIS (26/09/2026, diagnostic réel demandé par l'utilisateur - "et si y'a temps
# additionnel") : 110 minutes couvrait un match classique (90 min + arrêts de jeu, 100-105 min
# observés en pratique), mais PAS un match de coupe qui va en prolongation + tirs au but (Coupe
# du Monde, Ligue des Champions à élimination directe, CAN, Copa del Rey...). Sources concordantes
# vérifiées (plusieurs guides sportifs indépendants) : 90 min + prolongation (30 min) + tirs au but
# (10-20 min) + arrêts de jeu peut atteindre 150 à 165 minutes dans les cas extrêmes. Portée à 165
# minutes (2h45) : couvre confortablement ce maximum réaliste, tout en laissant encore une marge
# raisonnable avant le prochain match de la même équipe. Une vraie clôture (score reçu de l'API)
# continue de primer sur cette estimation dès qu'elle est disponible - cette valeur ne sert que de
# filet de sécurité tant qu'aucun score n'est encore arrivé.
DUREE_ESTIMEE_MATCH = timedelta(minutes=165)
# CORRECTIF NOBELLIS (audit, volume API 12 compétitions, 10/09/2026) : sans cette borne, un match
# prévu dans 8 mois (fin de saison, phase finale de Coupe du Monde, etc.) resterait visible sur
# l'Accueil dès aujourd'hui, mélangé sans ordre avec les milliers d'autres remontés par les 12
# compétitions. Fenêtre choisie par l'utilisateur : 72h. N'affecte QUE l'Accueil - rien n'est
# supprimé de Firestore ni de la mémoire, et un match plus lointain redevient visible tout seul
# dès qu'il entre dans cette fenêtre, sans aucune action manuelle.
DUREE_FENETRE_ACCUEIL_A_VENIR = timedelta(hours=72)


def _match_clos_depuis_moins_de(match: Optional[Dict[str, Any]], duree: timedelta) -> bool:
    """
    CORRECTIF NOBELLIS : logique de sécurité unique, partagée par deux règles distinctes -
    les 30 minutes de visibilité des analyses publiées ET les 5 minutes de visibilité des
    matchs sur l'Accueil. Une seule fonction à maintenir, deux usages avec des durées
    différentes, plutôt que deux implémentations quasi identiques risquant de diverger.

    Sécurité par défaut : match introuvable, statut "à venir", ou horodatage de clôture
    absent/corrompu -> True (toujours considéré comme "dans la fenêtre", donc visible).
    Une donnée mal formée doit échouer du côté visible, jamais du côté caché.
    """
    if not match:
        return True
    if match.get("statut", "a_venir") == "a_venir":
        return True

    cloture_brute = match.get("cloture_timestamp")
    if not cloture_brute:
        return True

    try:
        cloture_dt = datetime.fromisoformat(str(cloture_brute))
    except (ValueError, TypeError):
        return True

    return datetime.now(timezone.utc) - cloture_dt <= duree


_IDENTIFIANTS_MATCHS_DEMO = {"1", "2", "3", "4", "5", "6", "7"}


def _est_match_de_demonstration(match: Dict[str, Any]) -> bool:
    """
    CORRECTIF NOBELLIS (24/09/2026) : les matchs de démonstration (1 à 7) ont des états volontairement
    figés ou recalculés au démarrage (voir plus haut) - ils gardent EXACTEMENT leurs anciennes
    règles de visibilité. Les vrais matchs ont un identifiant "m_xxxxxxxxxxxx" (jamais "1" à "7").
    """
    return match.get("source") == "demo" or str(match.get("id")) in _IDENTIFIANTS_MATCHS_DEMO


def _lire_horodatage_utc(valeur: Any) -> Optional[datetime]:
    """
    CORRECTIF NOBELLIS (24/09/2026) : lit un horodatage ISO en datetime UTC, ou None si absent/illisible.
    Accepte le suffixe "Z" (format brut de football-data.org, non lu par fromisoformat avant
    Python 3.11) et suppose UTC pour un horodatage sans fuseau, jamais un plantage de comparaison.
    """
    try:
        lu = datetime.fromisoformat(str(valeur).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return lu if lu.tzinfo else lu.replace(tzinfo=timezone.utc)


# CORRECTIF NOBELLIS (26/09/2026, demande explicite de l'utilisateur : "je veux que tout respecte
# les règles") : ensemble RESTREINT, distinct de _IDENTIFIANTS_MATCHS_DEMO ci-dessus - seuls les
# matchs 4, 5 et 7 ont un état volontairement FIGÉ à la main (voir leur création plus haut) qui ne
# correspond PAS à la formule normale coup d'envoi + durée estimée (ex : match 5 "terminé" avec un
# coup d'envoi vieux de 3h mais une clôture d'il y a 2 minutes - la formule normale le jugerait
# déjà trop vieux). Leur appliquer la formule normale casserait ces démonstrations précises de la
# page Accueil. Les matchs 1, 2, 3 et 6 - les seuls concernés par cette demande, puisque ce sont
# les seuls ayant de vraies fiches de démonstration attachées (p_101, p_102, p_103) - suivent
# maintenant EXACTEMENT la même règle que les vrais matchs et disparaissent donc aussi avec le
# temps, une fois leur coup d'envoi (réactualisé à chaque redémarrage) suffisamment ancien.
_IDENTIFIANTS_MATCHS_DEMO_ETATS_FIGES = {"4", "5", "7"}


def _visible_jusqu_a_fin_de_match_plus(match: Dict[str, Any], duree: timedelta) -> bool:
    """
    CORRECTIF NOBELLIS (24/09/2026, "l'analyse doit disparaître après la fin du match") : vrai tant
    qu'on est à moins de `duree` de la FIN du match, la fin étant :
      - la fin ESTIMÉE (coup d'envoi + DUREE_ESTIMEE_MATCH) - l'app n'a aucun flux de score en
        direct, et la clôture réelle n'arrive parfois que le lendemain (synchronisation nocturne) ;
      - ramenée à l'instant de clôture si le match a été clôturé/annulé AVANT cette fin estimée
        (ex : match annulé à l'avance). Le plus petit des deux gagne : une clôture tardive ne fait
        JAMAIS réapparaître ce que l'estimation avait déjà caché.

    CORRECTIF NOBELLIS (26/09/2026) : SEULS les matchs de démo 4, 5 et 7 (état figé à la main, voir
    _IDENTIFIANTS_MATCHS_DEMO_ETATS_FIGES ci-dessus) gardent l'ancienne règle. Les matchs 1, 2, 3 et
    6 suivent désormais la RÈGLE NORMALE ci-dessous, comme n'importe quel vrai match - une fiche de
    démonstration disparaît donc aussi après sa fenêtre de visibilité, au lieu de rester affichée
    pour toujours.

    CORRECTIF NOBELLIS (26/09/2026, "et si le match a été reporté ?") : un match "reporte" (voir
    traiter_matchs_reportes_par_identifiant) garde son ANCIEN coup d'envoi pour référence, devenu
    obsolète - lui appliquer la formule normale le ferait disparaître à tort (l'ancienne date est
    déjà dépassée). Reste visible jusqu'à ce qu'une nouvelle date confirmée le fasse redevenir
    "a_venir" avec un coup d'envoi à jour - même principe que "match introuvable" plus bas : ne
    jamais cacher par excès de confiance dans une donnée qu'on sait déjà obsolète.

    Coup d'envoi absent/illisible : repli sur l'ancienne règle (visible par sécurité, jamais caché
    à tort faute de pouvoir juger).
    """
    if match.get("statut") == "reporte":
        return True

    if str(match.get("id")) in _IDENTIFIANTS_MATCHS_DEMO_ETATS_FIGES:
        return _match_clos_depuis_moins_de(match, duree)

    coup_d_envoi = _lire_horodatage_utc(match.get("kickoff_utc"))
    if coup_d_envoi is None:
        return _match_clos_depuis_moins_de(match, duree)

    fin = coup_d_envoi + DUREE_ESTIMEE_MATCH
    if match.get("statut", "a_venir") in ("termine", "annule"):
        cloture = _lire_horodatage_utc(match.get("cloture_timestamp"))
        if cloture is not None:
            fin = min(fin, cloture)

    return datetime.now(timezone.utc) - fin <= duree


def fiche_est_visible(match_id: Any, publie_le: Optional[str] = None) -> bool:
    """
    CORRECTIF NOBELLIS : une analyse reste consultable jusqu'à 30 minutes après la FIN de son
    match, puis devient inaccessible - comme sur les plateformes de paris de référence, un profil
    d'analyste ne s'encombre pas indéfiniment de fiches closes depuis des semaines.

    CORRECTIF NOBELLIS (24/09/2026) : la fin du match n'est plus l'instant de clôture (une action
    manuelle ou la synchronisation nocturne, parfois des heures ou des semaines après le match -
    des analyses restaient affichées 13 jours) mais la fin ESTIMÉE, ramenée à la clôture si elle
    est plus précoce - voir _visible_jusqu_a_fin_de_match_plus. Match introuvable : visible par
    sécurité, comme avant.

    CORRECTIF NOBELLIS (26/09/2026, fiches orphelines) : bug réel trouvé en production - une fiche
    publiée le 11/09/2026 restait visible indéfiniment car son match_id ne correspondait plus à
    aucun match (très probablement suite à la migration de stockage Firestore de ce même jour, qui
    a régénéré les identifiants). "Match introuvable -> visible par sécurité" est correct à COURT
    terme (le match n'est peut-être pas encore chargé en mémoire), mais devient un piège permanent
    si l'identifiant a disparu pour de bon. Avec `publie_le` fourni, une fiche orpheline applique
    désormais la MÊME fenêtre totale qu'un match normal (durée estimée d'un match + 30 min) à
    partir de sa date de publication, puis se ferme comme les autres - au lieu de rester bloquée
    pour toujours. Sans `publie_le` (appel historique non mis à jour) : ancien comportement
    inchangé, visible par sécurité.

    N'efface JAMAIS aucune donnée - seulement l'accès. Les statistiques de l'analyste (précision,
    meilleure série, classement) continuent de se calculer sur l'historique complet et intact.
    """
    match = next((m for m in DATA_MATCHS if m.get("id") == str(match_id)), None)
    if match:
        return _visible_jusqu_a_fin_de_match_plus(match, DUREE_VISIBILITE_APRES_CLOTURE)

    if publie_le:
        publication_dt = _lire_horodatage_utc(publie_le)
        if publication_dt is not None:
            fenetre_totale = DUREE_ESTIMEE_MATCH + DUREE_VISIBILITE_APRES_CLOTURE
            return datetime.now(timezone.utc) - publication_dt <= fenetre_totale

    return True


def _cle_tri_par_coup_denvoi(match: Dict[str, Any]) -> datetime:
    """
    CORRECTIF NOBELLIS (audit, tri chronologique) : clé de tri partagée - le match le plus proche
    dans le temps en premier. Un horodatage absent ou corrompu est repoussé en fin de liste
    (jamais mélangé au hasard parmi des dates réelles), cohérent avec le principe de repli sûr
    déjà appliqué dans match_visible_sur_accueil. Utilisée par route_accueil ET route_analyse -
    définie une seule fois ici pour ne jamais risquer une divergence de comportement entre les deux.
    """
    try:
        return datetime.fromisoformat(str(match.get("kickoff_utc")))
    except (ValueError, TypeError):
        return datetime.max.replace(tzinfo=timezone.utc)


def match_visible_sur_accueil(match: Dict[str, Any]) -> bool:
    """
    CORRECTIF NOBELLIS (audit, faille n°12) : un match reste visible sur l'Accueil jusqu'à 30
    minutes après sa VRAIE fin - qu'elle soit connue via une clôture manuelle (ou, demain,
    automatiquement via une API football), ou, à défaut, estimée à partir du coup d'envoi +
    DUREE_ESTIMEE_MATCH. Avant ce correctif, un match jamais clôturé manuellement restait affiché
    indéfiniment (statut "à venir" pour toujours) : ce cas est maintenant couvert par
    l'estimation, sans jamais dépendre d'une action admin qui pourrait ne jamais arriver.

    Priorité stricte : une vraie clôture (cloture_timestamp réellement défini) l'emporte toujours
    sur l'estimation - le jour où l'API alimente automatiquement cette donnée, elle prend le
    dessus sans aucune modification de code nécessaire ici.

    Exception : le match "4" des données de démonstration reste volontairement figé en statut
    "à venir" avec un coup d'envoi déjà passé, pour démontrer indéfiniment l'état "résultat en
    attente" - il est explicitement exclu de cette règle pour ne jamais casser cette démonstration.

    CORRECTIF NOBELLIS (26/09/2026, "et si le match a été reporté ?") : un match "reporte" garde
    son ancien coup d'envoi pour référence, déjà obsolète - jamais utilisé ici pour le juger "trop
    vieux" et le cacher à tort. Reste visible sur l'Accueil jusqu'à nouvelle date confirmée (voir
    traiter_matchs_reportes_par_identifiant et fiche_est_visible, même principe).
    """
    if not match:
        return True
    if match.get("id") == "4":
        return True
    if match.get("statut") == "reporte":
        return True

    if match.get("statut", "a_venir") in ("termine", "annule"):
        # CORRECTIF NOBELLIS (24/09/2026) : même règle que les analyses - une clôture tardive (ex :
        # synchronisation nocturne) ne fait plus réapparaître 30 minutes un match que l'estimation
        # de fin avait déjà caché ci-dessous.
        return _visible_jusqu_a_fin_de_match_plus(match, DUREE_VISIBILITE_ACCUEIL_APRES_CLOTURE)

    # Statut encore "à venir", jamais clôturé manuellement : on se rabat sur l'estimation.
    kickoff_brut = match.get("kickoff_utc")
    if not kickoff_brut:
        return True  # Donnée absente/corrompue -> toujours visible par sécurité, jamais caché à tort.
    try:
        kickoff_dt = datetime.fromisoformat(str(kickoff_brut))
    except (ValueError, TypeError):
        return True

    fin_estimee = kickoff_dt + DUREE_ESTIMEE_MATCH
    if datetime.now(timezone.utc) - fin_estimee > DUREE_VISIBILITE_ACCUEIL_APRES_CLOTURE:
        return False  # Déjà fini depuis trop longtemps - logique originale, inchangée.

    # CORRECTIF NOBELLIS (audit, volume API 12 compétitions) : un match dont le coup d'envoi est
    # encore trop loin dans le futur n'apparaît pas tout de suite - voir DUREE_FENETRE_ACCUEIL_A_VENIR.
    if kickoff_dt - datetime.now(timezone.utc) > DUREE_FENETRE_ACCUEIL_A_VENIR:
        return False

    return True


def match_encore_analysable_par_ia(match: Dict[str, Any]) -> bool:
    """
    CORRECTIF NOBELLIS : générer une simulation Monte Carlo pré-match n'a aucun sens dès que
    le coup d'envoi réel est passé - que l'admin ait eu le temps de clôturer officiellement le
    match ou non. Contrairement aux deux fonctions ci-dessus (ancrées sur cloture_timestamp,
    une action manuelle qui peut tarder des heures), celle-ci s'ancre sur kickoff_utc - une
    donnée fixée dès la création du match, connue à l'avance, qui ne dépend d'aucune action
    admin ultérieure. Horodatage absent ou corrompu -> False par sécurité : on ne génère jamais
    une fausse prédiction faute de pouvoir vérifier que le match n'a pas déjà commencé.
    """
    # CORRECTIF NOBELLIS : un match annulé n'a plus rien à analyser, même annoncé bien avant le
    # coup d'envoi - cette vérification manquait, un match annulé à l'avance restait offert à la
    # publication et au moteur IA jusqu'à son heure de coup d'envoi théorique (trouvé et prouvé
    # par test avant correction).
    if match.get("statut", "a_venir") in ("termine", "annule"):
        return False
    try:
        kickoff_dt = datetime.fromisoformat(str(match.get("kickoff_utc")))
    except (ValueError, TypeError):
        return False
    return datetime.now(timezone.utc) < kickoff_dt


def calculer_matchs_deja_publies() -> set:
    """
    CORRECTIF NOBELLIS : détection des matchs sur lesquels l'analyste actuellement connecté a déjà
    une analyse active - alimente le badge visuel et la fenêtre de confirmation avant modification,
    pour que personne ne remplace une analyse existante sans le savoir clairement à l'avance.
    Factorisée ici (au lieu d'être recalculée séparément à plusieurs endroits) pour que
    route_analystes et /api/publier/formulaire/<match_id> utilisent toujours exactement le même
    résultat - jamais deux calculs qui pourraient diverger avec le temps.
    """
    id_analyste_connecte = DATA_PROFIL.get("analyst_id")
    if not id_analyste_connecte:
        return set()
    return {
        p.get("match_id") for p in DATA_PREDICTIONS_EXPERT.values()
        if p.get("expert_id") == id_analyste_connecte
    }


def obtenir_matchs_visibles() -> List[Dict[str, Any]]:
    """
    CORRECTIF NOBELLIS (audit, faille n°8) : tant qu'aucun vrai match fourni par une API football
    n'existe (source == "api"), tous les matchs de démonstration (source == "demo") restent
    affichés, pour continuer à pouvoir tester l'application normalement. Dès qu'au moins UN vrai
    match "api" existe, les matchs de démo sont automatiquement masqués de l'affichage - un
    remplacement propre et immédiat, sans aucune action manuelle et sans supprimer les données
    de démonstration elles-mêmes (elles restent en mémoire/Firestore, simplement non affichées).
    """
    matchs_api = [m for m in DATA_MATCHS if m.get("source") == "api"]
    if matchs_api:
        return matchs_api
    return list(DATA_MATCHS)


def annuler_pronostics_du_match(match_id: str) -> None:
    """
    CORRECTIF NOBELLIS : Statut "annulé" pour les matchs interrompus, reportés ou annulés.
    Les pronostics liés ne sont ni gagnés ni perdus - ils sont exclus du calcul de précision
    pour ne pas pénaliser injustement un analyste sur un événement hors de son contrôle.
    """
    for prono_id, prediction_data in DATA_PREDICTIONS_EXPERT.items():
        if str(prediction_data.get("match_id")) != str(match_id):
            continue
        prediction_data["resultat"] = "annule"

        expert_id = prediction_data.get("expert_id")
        analyste_proprietaire = next((a for a in DATA_ANALYSTES if a["id"] == expert_id), None)
        if analyste_proprietaire:
            fiche_affichee = next((f for f in analyste_proprietaire.get("liste_pronostics", []) if f.get("id") == prono_id), None)
            if fiche_affichee:
                fiche_affichee["resultat"] = "annule"

    recalculer_stats_tous_analystes()


def cloturer_matchs_par_identifiant_football_data(resultats_par_id_api: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """
    AJOUT NOBELLIS (24/09/2026, Partie 2) : clôture AUTORITÉ PRINCIPALE, par IDENTIFIANT EXACT
    (id_api, stocké sur chaque match à sa création par synchroniser_matchs_api.py) - remplace la
    correspondance par noms d'équipes + fenêtre de date de cloturer_matchs_automatiquement pour
    tout match qui possède un id_api : deux matchs distincts ne peuvent jamais partager le même
    identifiant, contrairement à deux matchs des mêmes équipes à des dates différentes (voir
    l'incident du 24/09/2026 qui a motivé TOLERANCE_DATE_RESULTAT_API ci-dessous). Un match SANS
    id_api (ancien match importé à la main, match de démonstration) n'est jamais concerné ici -
    voir cloturer_matchs_automatiquement, conservée comme repli pour ces cas précis.

    Même seuil d'éligibilité que la clôture par noms (3h après le coup d'envoi). N'efface jamais
    aucune donnée, ne clôture jamais sans score exploitable (voir
    recuperer_resultats_par_identifiant : score_90min peut être None si "score.fullTime" est
    absent/incomplet - le match reste alors "à venir", visible dans la liste des matchs bloqués).

    Traçabilité posée sur chaque match clôturé ici (cohérente avec cloturer_matchs_automatiquement
    plus bas) : cloture_source="football_data_id", cloture_statut_brut=le "score.duration" brut
    de football-data.org (ex: "REGULAR", "EXTRA_TIME", "PENALTY_SHOOTOUT").
    """
    nb_clotures = 0
    nb_sans_score_exploitable = 0
    maintenant = datetime.now(timezone.utc)

    with _VERROU_MUTATION_ETAT:
        for match in DATA_MATCHS:
            if match.get("statut", "a_venir") != "a_venir" or _est_match_de_demonstration(match):
                continue
            id_api = match.get("id_api")
            if id_api is None or id_api not in resultats_par_id_api:
                continue

            kickoff_dt = _lire_horodatage_utc(match.get("kickoff_utc"))
            if kickoff_dt is None or maintenant - kickoff_dt <= timedelta(hours=3):
                continue  # Pas encore éligible - même seuil que la clôture par noms.

            infos = resultats_par_id_api[id_api]
            score_90min = infos.get("score_90min")
            if score_90min is None:
                nb_sans_score_exploitable += 1
                print(f"⚠️  [NOBELLIS] Match {match['home']} vs {match['away']} (id_api={id_api}) trouvé "
                      f"FINISHED mais sans score exploitable ('score.fullTime' absent/incomplet) - "
                      f"laissé pour clôture manuelle.", file=sys.stderr)
                continue

            score_final = f"{score_90min[0]}-{score_90min[1]}"
            match["statut"] = "termine"
            match["score_final"] = score_final
            match["cloture_timestamp"] = maintenant.isoformat()
            match["cloture_automatique"] = True
            match["cloture_source"] = "football_data_id"
            match["cloture_statut_brut"] = infos.get("statut_brut")
            resoudre_pronostics_du_match(match["id"], score_final)
            nb_clotures += 1
            print(f"✅ [NOBELLIS] Clôture par identifiant : {match['home']} {score_final} "
                  f"{match['away']} (id_api={id_api}, statut brut : {infos.get('statut_brut')}).", file=sys.stderr)

        # CORRECTIF NOBELLIS : même principe que cloturer_matchs_automatiquement (audit du
        # 16/09/2026) - le snapshot Firestore est pris PENDANT qu'on tient encore le verrou.
        if nb_clotures > 0:
            _synchroniser_firestore_arriere_plan(
                data_matchs=DATA_MATCHS,
                data_predictions_expert=DATA_PREDICTIONS_EXPERT,
                data_analystes=DATA_ANALYSTES,
            )

    return {"nb_clotures": nb_clotures, "nb_sans_score_exploitable": nb_sans_score_exploitable}


def traiter_matchs_reportes_par_identifiant(matchs_reportes_par_id_api: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """
    AJOUT NOBELLIS (26/09/2026, "et si le match a été reporté ?") : marque comme "reporte" tout
    match encore "a_venir" dont l'identifiant football-data.org (id_api) est trouvé POSTPONED
    aujourd'hui. Un match de démonstration n'a jamais d'id_api : jamais concerné ici, aucune
    vérification supplémentaire nécessaire.

    Pourquoi un nouveau statut plutôt que de simplement corriger kickoff_utc : le report ne donne
    PAS de nouvelle date connue (l'API renvoie juste "reporté", sans indiquer quand). Continuer à
    juger sa visibilité sur l'ancien coup d'envoi (déjà dépassé) le ferait disparaître à tort,
    exactement le problème signalé. "reporte" fait sortir le match de la formule normale coup
    d'envoi + durée estimée (voir _visible_jusqu_a_fin_de_match_plus et match_visible_sur_accueil)
    - il reste visible, comme n'importe quelle donnée qu'on ne peut pas encore juger avec
    certitude, jusqu'à ce qu'une nouvelle date soit confirmée.

    L'ancien coup d'envoi est conservé tel quel (jamais écrasé) - trace utile, et redevient la
    base de calcul normale dès que la ligue annonce une nouvelle date confirmée : voir
    synchroniser_tous_les_matchs (synchroniser_matchs_api.py), qui reconnaît un match "reporte"
    déjà connu et remet à jour sa date + son statut "a_venir" dès qu'elle change, SANS jamais créer
    de doublon.

    Idempotent et sûr à rappeler chaque nuit : un match déjà "reporte" n'est plus "a_venir" et
    n'est donc plus concerné par ce filtre, jamais re-traité inutilement.
    """
    nb_matchs_reportes = 0
    maintenant = datetime.now(timezone.utc)

    with _VERROU_MUTATION_ETAT:
        for match in DATA_MATCHS:
            if match.get("statut", "a_venir") != "a_venir" or _est_match_de_demonstration(match):
                continue
            id_api = match.get("id_api")
            if id_api is None or id_api not in matchs_reportes_par_id_api:
                continue

            match["statut"] = "reporte"
            match["date_report_detectee"] = maintenant.isoformat()
            nb_matchs_reportes += 1
            print(f"⏸️  [NOBELLIS] Match reporté détecté : {match['home']} vs {match['away']} "
                  f"(id_api={id_api}) - ancien coup d'envoi conservé pour référence, fiche gardée "
                  f"visible jusqu'à nouvelle date confirmée.", file=sys.stderr)

        if nb_matchs_reportes > 0:
            _synchroniser_firestore_arriere_plan(
                data_matchs=DATA_MATCHS,
                data_predictions_expert=DATA_PREDICTIONS_EXPERT,
                data_analystes=DATA_ANALYSTES,
            )

    return {"nb_matchs_reportes": nb_matchs_reportes}


# CORRECTIF NOBELLIS (24/09/2026) : écart maximal accepté entre le coup d'envoi enregistré par
# Nobellis et la date du résultat fourni par l'API pour qu'ils désignent le MÊME match. Deux
# rencontres des mêmes équipes ne se jouent jamais à moins de 12 h d'intervalle ; un léger report
# de la journée reste accepté. Au-delà, jamais de clôture automatique (voir plus bas).
TOLERANCE_DATE_RESULTAT_API = timedelta(hours=12)


def cloturer_matchs_automatiquement(matchs_pour_cloture: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    CORRECTIF NOBELLIS (Bloc 2, clôture automatique, 13/09/2026) : clôture automatiquement les
    matchs dont le résultat réel a été confirmé par synchroniser_resultats_api_football.py -
    AUCUN appel API supplémentaire ici, `matchs_pour_cloture` a déjà été récupéré par ce script
    dans le même cycle quotidien (voir _boucle_synchronisation_api_quotidienne).

    Correspondance par (home, away) - noms Nobellis, garantis cohérents entre les deux
    fournisseurs par la Solution 1 (verifier_coherence_noms.py). RÈGLE D'AMBIGUÏTÉ STRICTE :
    si plusieurs résultats du jour correspondent au même couple (home, away), le match est laissé
    de côté pour une clôture manuelle - jamais un score deviné au hasard entre plusieurs
    candidats. Même seuil d'éligibilité que la clôture manuelle (3h après le coup d'envoi, voir
    api_admin_matchs_a_cloturer) - pour rester cohérent avec ce que l'admin voit déjà.

    CORRECTIF NOBELLIS (24/09/2026, garde de date) : la correspondance par noms d'équipes ne suffit
    pas - un ancien match resté "à venir" pouvait recevoir le score d'un AUTRE match des mêmes
    équipes joué la veille. Un résultat n'est donc candidat que si sa date (date_api_football) est à
    moins de TOLERANCE_DATE_RESULTAT_API du coup d'envoi enregistré ; une date absente ou illisible
    l'écarte (mieux vaut aucun score qu'un faux score). Le match reste alors visible dans la liste
    des matchs à clôturer à la main et sur la page de santé.

    Réutilise EXACTEMENT la même logique que api_admin_cloturer_match : mêmes champs mis à jour
    (statut, score_final, cloture_timestamp), même résolution des pronostics des analystes - pour
    qu'un match clôturé automatiquement soit strictement indiscernable d'un match clôturé à la
    main, à ceci près qu'il porte un champ `cloture_automatique: True` pour la traçabilité.
    """
    maintenant = datetime.now(timezone.utc)
    nb_clotures = 0
    nb_ambigus = 0
    nb_hors_date = 0

    resultats_par_couple: Dict[Any, List[Dict[str, Any]]] = {}
    for r in matchs_pour_cloture:
        cle = (r["home"], r["away"])
        resultats_par_couple.setdefault(cle, []).append(r)

    with _VERROU_MUTATION_ETAT:
        for match in DATA_MATCHS:
            if match.get("statut") != "a_venir" or match.get("source") != "api":
                continue
            kickoff_dt = _lire_horodatage_utc(match.get("kickoff_utc"))
            if kickoff_dt is None:
                continue
            if maintenant - kickoff_dt <= timedelta(hours=3):
                continue  # Pas encore éligible - même seuil que la clôture manuelle.

            meme_couple = resultats_par_couple.get((match["home"], match["away"]), [])

            if len(meme_couple) == 0:
                continue  # Pas encore de résultat trouvé (hier/aujourd'hui) - réessayé au prochain cycle.

            candidats = []
            for resultat_api in meme_couple:
                date_resultat = _lire_horodatage_utc(resultat_api.get("date_api_football"))
                if date_resultat is not None and abs(date_resultat - kickoff_dt) <= TOLERANCE_DATE_RESULTAT_API:
                    candidats.append(resultat_api)
            if len(candidats) == 0:
                nb_hors_date += 1
                print(f"⚠️  [NOBELLIS] Clôture automatique refusée pour {match['home']} vs {match['away']} "
                      f"(coup d'envoi {match.get('kickoff_utc')}) : {len(meme_couple)} résultat(s) pour ce duel, "
                      f"aucun à moins de {TOLERANCE_DATE_RESULTAT_API.total_seconds() / 3600:.0f}h de ce coup d'envoi - "
                      f"laissé pour clôture manuelle.", file=sys.stderr)
                continue
            if len(candidats) > 1:
                nb_ambigus += 1
                print(f"⚠️  [NOBELLIS] Clôture automatique ambiguë pour {match['home']} vs {match['away']} "
                      f"({len(candidats)} résultats candidats le même jour) - laissé pour clôture manuelle.",
                      file=sys.stderr)
                continue

            score_final = candidats[0]["score_final"]
            match["statut"] = "termine"
            match["score_final"] = score_final
            match["cloture_timestamp"] = maintenant.isoformat()
            match["cloture_automatique"] = True
            # AJOUT NOBELLIS (24/09/2026, traçabilité) : mêmes noms de champs que
            # cloturer_matchs_par_identifiant_football_data - cette voie (nom + date) n'est plus
            # utilisée qu'en REPLI, pour les matchs sans id_api (voir docstring plus haut).
            match["cloture_source"] = "api_football_nom_date"
            match["cloture_statut_brut"] = candidats[0].get("statut_api_football")
            resoudre_pronostics_du_match(match["id"], score_final)
            nb_clotures += 1

        # CORRECTIF NOBELLIS (audit, cohérence avec api_admin_cloturer_match, 16/09/2026) :
        # déplacé À L'INTÉRIEUR du verrou - le deepcopy de _synchroniser_firestore_arriere_plan
        # doit se faire pendant qu'on tient encore le verrou (voir son docstring), exactement
        # comme le fait déjà api_admin_cloturer_match. Appelé hors du verrou, un thread
        # concurrent pouvait muter DATA_MATCHS/DATA_PREDICTIONS_EXPERT/DATA_ANALYSTES pendant
        # la copie -> snapshot Firestore incohérent, jamais acceptable.
        if nb_clotures > 0:
            _synchroniser_firestore_arriere_plan(
                data_matchs=DATA_MATCHS,
                data_predictions_expert=DATA_PREDICTIONS_EXPERT,
                data_analystes=DATA_ANALYSTES,
            )

    return {"nb_clotures": nb_clotures, "nb_ambigus": nb_ambigus, "nb_hors_date": nb_hors_date}


def _lister_matchs_api_bloques(maintenant: datetime) -> List[Dict[str, Any]]:
    """
    AJOUT NOBELLIS (24/09/2026) : matchs RÉELS (source "api") encore "à venir" plus de 3 h après leur
    coup d'envoi - même seuil que la clôture manuelle et automatique. Ce sont les matchs dont le
    résultat n'a jamais été confirmé : leurs analyses restent "en attente" et les statistiques des
    analystes ne progressent pas tant qu'ils ne sont pas clôturés. Du plus ancien au plus récent.
    Lecture seule de la mémoire, aucun appel réseau.
    """
    bloques = []
    for match in DATA_MATCHS:
        if match.get("statut") != "a_venir" or match.get("source") != "api":
            continue
        kickoff_dt = _lire_horodatage_utc(match.get("kickoff_utc"))
        if kickoff_dt is None or maintenant - kickoff_dt <= timedelta(hours=3):
            continue
        bloques.append({
            "id": match.get("id"),
            "match_nom": f"{match.get('home')} vs {match.get('away')}",
            "kickoff_utc": match.get("kickoff_utc"),
            "heures_depuis_coup_d_envoi": round((maintenant - kickoff_dt).total_seconds() / 3600, 1),
        })
    return sorted(bloques, key=lambda b: b["heures_depuis_coup_d_envoi"], reverse=True)


def recalculer_stats_tous_analystes() -> None:
    """
    CORRECTIF NOBELLIS : Recalcul du taux de victoire réel de chaque analyste,
    uniquement à partir de ses pronostics effectivement résolus (les pronostics "annulés"
    ne comptent ni comme gagnés ni comme perdus). Jamais de chiffre inventé -
    "precision" reste 0 (affiché comme repli honnête côté frontend) tant qu'aucun pronostic n'est résolu.

    CORRECTIF NOBELLIS : Certification méritée. Le badge n'est plus accordé au seul volume :
    il faut à la fois un volume minimum ET une précision minimum mesurée par la borne basse
    de Wilson (résistante à la chance sur petit échantillon), recalculée à chaque clôture.
    Une hystérésis empêche le badge de "clignoter" au moindre match : un changement de palier
    doit être confirmé deux recalculs de suite avant d'être appliqué à l'affichage.
    """
    for analyste in DATA_ANALYSTES:
        pronostics_lies = [d for d in DATA_PREDICTIONS_EXPERT.values() if d.get("expert_id") == analyste["id"]]
        resolus = [d for d in pronostics_lies if d.get("resultat") in ("gagne", "perdu")]
        gagnes = [d for d in resolus if d.get("resultat") == "gagne"]

        analyste["pronostics_resolus"] = len(resolus)
        analyste["pronostics_gagnes"] = len(gagnes)
        analyste["precision"] = round(len(gagnes) / len(resolus) * 100) if resolus else 0

        score_wilson = calculer_score_wilson(len(gagnes), len(resolus))
        analyste["wilson_score"] = score_wilson

        # Détermination du palier mérité selon les règles strictes (volume ET précision).
        if len(resolus) < SEUIL_VOLUME_MINIMUM:
            palier_calcule = "Nouveau"
        elif score_wilson >= SEUIL_WILSON_ELITE:
            palier_calcule = "Élite"
        elif score_wilson >= SEUIL_WILSON_CERTIFIE:
            palier_calcule = "Certifié"
        else:
            palier_calcule = "Nouveau"

        palier_affiche_actuel = analyste.get("badge_nobellis", "Nouveau")
        palier_en_attente = analyste.get("_palier_en_attente")

        if palier_calcule == palier_affiche_actuel:
            # Rien à confirmer : le palier calculé correspond déjà à l'affiché.
            analyste["_palier_en_attente"] = None
        elif palier_calcule == palier_en_attente:
            # Confirmé deux fois de suite : le changement de palier s'applique.
            analyste["badge_nobellis"] = palier_calcule
            analyste["_palier_en_attente"] = None
        else:
            # Première apparition de ce nouveau palier : mise en attente de confirmation.
            analyste["_palier_en_attente"] = palier_calcule

        # CORRECTIF NOBELLIS : le contour visuel doré (certifie) suit désormais la même règle
        # que le badge textuel - jamais de contradiction entre le style et le texte affiché.
        analyste["certifie"] = analyste["badge_nobellis"] != "Nouveau"


def calculer_serie_et_forme_recente(analyste_id: Optional[str]) -> Dict[str, Any]:
    """
    CORRECTIF NOBELLIS : "Meilleure Série" (plus longue suite de victoires consécutives) et
    "Forme Récente" (5 dernières analyses résolues, dans l'ordre chronologique) - purement dérivés
    des pronostics déjà résolus de l'analyste, aucune règle métier à inventer contrairement au
    R.O.I. virtuel (qui suppose une mise et une cote non définies). Les pronostics "en_attente" et
    "annule" sont exclus du calcul de série - un match non joué ne casse ni ne construit une série,
    exactement le même principe déjà appliqué au calcul de précision.
    """
    if not analyste_id:
        return {"meilleure_serie": 0, "forme_recente": []}

    pronostics_resolus = sorted(
        [
            d for d in DATA_PREDICTIONS_EXPERT.values()
            if d.get("expert_id") == analyste_id and d.get("resultat") in ("gagne", "perdu")
        ],
        key=lambda d: d.get("publie_le", "")
    )

    meilleure_serie = 0
    serie_courante = 0
    for pronostic in pronostics_resolus:
        if pronostic["resultat"] == "gagne":
            serie_courante += 1
            meilleure_serie = max(meilleure_serie, serie_courante)
        else:
            serie_courante = 0

    forme_recente = [p["resultat"] for p in pronostics_resolus[-5:]]

    return {"meilleure_serie": meilleure_serie, "serie_courante": serie_courante, "forme_recente": forme_recente}


# CORRECTIF NOBELLIS (audit, nouveau système de classement) : fenêtre de grâce avant que
# l'inactivité commence à peser sur le classement d'un analyste - volontairement en dehors de la
# fonction pour rester facilement ajustable sans devoir replonger dans la logique de calcul.
JOURS_GRACE_AVANT_DECLIN_ACTIVITE = 7


def calculer_coefficient_activite(analyste: Dict[str, Any]) -> float:
    """
    CORRECTIF NOBELLIS (audit, nouveau système de classement) : mesure la régularité récente d'un
    analyste sous forme d'un coefficient CONTINU entre 0 et 1 - jamais un interrupteur qui bascule
    brutalement d'éligible à inéligible. Tant que la dernière publication remonte à
    JOURS_GRACE_AVANT_DECLIN_ACTIVITE jours ou moins, le coefficient reste à son maximum (1.0).
    Au-delà, il diminue progressivement (division par deux tous les 7 jours de retard supplémentaire)
    - jamais de chute brutale, jamais de valeur strictement nulle, pour qu'un analyste qui reprend
    une activité normale puisse toujours remonter en douceur, sans repartir de zéro. La continuité à
    la frontière des JOURS_GRACE_AVANT_DECLIN_ACTIVITE jours est volontaire (1.0 pile à la limite,
    puis décroissance douce juste après) - aucun saut de valeur à cet instant précis.
    """
    fiches = analyste.get("liste_pronostics") or []
    dates_publication = [f.get("publie_le") for f in fiches if f.get("publie_le")]
    if not dates_publication:
        return 1.0  # Analyste tout juste éligible (vient de publier) - pas encore de recul, pas de pénalité.

    try:
        derniere_publication = max(datetime.fromisoformat(str(d)) for d in dates_publication)
    except (ValueError, TypeError):
        return 1.0

    jours_ecoules = (datetime.now(timezone.utc) - derniere_publication).total_seconds() / 86400
    if jours_ecoules <= JOURS_GRACE_AVANT_DECLIN_ACTIVITE:
        return 1.0

    jours_de_retard = jours_ecoules - JOURS_GRACE_AVANT_DECLIN_ACTIVITE
    return 0.5 ** (jours_de_retard / 7.0)


def calculer_score_classement_nobellis(analyste: Dict[str, Any]) -> float:
    """
    CORRECTIF NOBELLIS (audit, nouveau système de classement) : score de mérite unique et continu,
    qui remplace l'ancien tri figé. Aucune position n'est jamais acquise - ce score est recalculé
    à chaque affichage, à partir de trois axes, tous continus (jamais de règle qui bascule d'un
    coup) :
      1. La fiabilité prouvée (score de Wilson, résistant à la chance sur petit échantillon).
      2. La régularité gagnante RÉCENTE (série de victoires EN COURS, pas l'ancien record) - monte
         en douceur de 0 à 1 entre 0 et 5 victoires d'affilée (le seuil de 5 demandé n'est donc pas
         un couperet, mais un palier où cet axe atteint son maximum).
      3. L'activité récente (coefficient continu ci-dessus) - agit comme un MULTIPLICATEUR sur
         l'ensemble : un analyste qui cesse de publier voit tout son score baisser progressivement,
         jour après jour, jusqu'à se faire dépasser un par un par des analystes actifs juste derrière
         lui - jamais une chute brutale en bas de classement.
    """
    facteur_forme = min(analyste.get("_serie_courante_calculee", 0) / 5.0, 1.0)
    score_de_base = (analyste.get("wilson_score", 0.0) * 0.5) + (facteur_forme * 0.5)
    return score_de_base * calculer_coefficient_activite(analyste)


def calculer_progression_palier(analyste: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    CORRECTIF NOBELLIS : progression réelle vers le palier supérieur (Nouveau -> Certifié -> Élite),
    dérivée des MÊMES seuils que la certification effective (SEUIL_VOLUME_MINIMUM, SEUIL_WILSON_ELITE)
    - jamais une seconde règle divergente inventée pour l'affichage. Repli honnête à 0% pour un
    profil sans historique, jamais un chiffre arbitraire pour "faire joli".
    """
    if not analyste:
        return {"rang_actuel": "Utilisateur Standard", "progression_pct": 0}

    palier_actuel = analyste.get("badge_nobellis", "Nouveau")
    resolus = analyste.get("pronostics_resolus", 0)
    wilson = analyste.get("wilson_score", 0.0)

    if palier_actuel == "Élite":
        # Déjà au sommet - la "progression" ne peut plus représenter une montée de palier.
        return {"rang_actuel": "Élite", "progression_pct": 100}

    if palier_actuel == "Nouveau":
        # Progression vers "Certifié" : uniquement une question de volume résolu.
        progression_pct = round(min(100, (resolus / SEUIL_VOLUME_MINIMUM) * 100)) if SEUIL_VOLUME_MINIMUM else 0
        return {"rang_actuel": "Nouveau", "progression_pct": progression_pct}

    # palier_actuel == "Certifié" : progression vers "Élite", bornée par le score de Wilson requis.
    progression_pct = round(min(100, (wilson / SEUIL_WILSON_ELITE) * 100)) if SEUIL_WILSON_ELITE else 0
    return {"rang_actuel": "Certifié", "progression_pct": progression_pct}


# CORRECTIF NOBELLIS : verrou anti-course pour la création/réactivation de profil analyste -
# empêche deux requêtes quasi simultanées de créer deux profils dupliqués pour la même personne.
_VERROU_CREATION_ANALYSTE = threading.Lock()


def obtenir_identifiant_profil_sur() -> str:
    """
    CORRECTIF NOBELLIS : accès sûr à l'identifiant du profil, jamais par indexation directe
    (DATA_PROFIL['id']) qui provoquerait une KeyError si un futur profil multi-utilisateur
    était initialisé sans cette clé. Si absent, génère UNE SEULE FOIS un identifiant unique et
    le persiste immédiatement sur DATA_PROFIL - empêche à la fois qu'un repli statique fasse
    collisionner plusieurs utilisateurs sur le même fichier, et qu'un repli régénéré à chaque
    appel change d'identité à chaque requête (ce qui serait pire que le bug d'origine).
    """
    identifiant = DATA_PROFIL.get('id')
    if not identifiant:
        identifiant = f"USR-{secrets.token_hex(6).upper()}"
        DATA_PROFIL['id'] = identifiant
    return identifiant


@app.route('/api/profil/devenir-analyste', methods=['POST'])
def api_devenir_analyste():
    """
    CORRECTIF NOBELLIS : Parcours "Devenir Analyste" - badge de départ honnête ("Nouveau"),
    aucune statistique inventée pour un tout nouveau profil.

    CORRECTIF NOBELLIS : réactivation sans duplication. Si un profil analyste existe déjà
    pour cet utilisateur (même inactif), il est RÉACTIVÉ - jamais recréé. Ferme la faille où
    "quitter puis revenir" permettrait de repartir avec un historique/badge vierge, contournant
    la certification méritée (Wilson). Cooldown de 48h après une désactivation, basé EXCLUSIVEMENT
    sur un horodatage écrit côté serveur (jamais une valeur transmise par le client) - empêche
    toute falsification du délai.

    CORRECTIF NOBELLIS : rate-limiting ajouté - cette route en était dépourvue alors que toutes
    les autres routes sensibles du profil l'ont, incohérence corrigée.
    """
    if not verifier_rate_limit(request.remote_addr, max_requetes=5):
        return jsonify({"succes": False, "erreur": "Trop de requêtes."}), 429

    # CORRECTIF NOBELLIS (comptes séparés, Lot 3, 16/09/2026) : profil du visiteur courant,
    # jamais plus DATA_PROFIL (singulier, partagé). Récupéré ICI, avant tout verrou -
    # obtenir_profil_courant() prend déjà _VERROU_MUTATION_ETAT en interne (voir
    # api_reinitialiser_profil pour le détail du risque d'interblocage sinon).
    profil_visiteur = obtenir_profil_courant()

    data: Dict[str, Any] = request.get_json(silent=True) or {}
    # CORRECTIF NOBELLIS : filtrage contre la liste fermée AVANT toute autre logique - la valeur
    # brute envoyée par le client n'est jamais utilisée telle quelle, seule sa version validée l'est.
    specialite = valider_specialite(str(data.get("specialite", "")).strip()[:200])

    # CORRECTIF NOBELLIS : ordre des contrôles inversé - l'état du compte (déjà analyste ?)
    # doit toujours être vérifié AVANT la validité des données saisies. Auparavant, un analyste
    # déjà actif qui omettait la spécialité recevait le message trompeur "spécialité manquante"
    # au lieu du message correct "vous êtes déjà analyste".
    if profil_visiteur.get("is_analyst"):
        return jsonify({"succes": False, "erreur": "Vous êtes déjà Analyste Certifié."}), 400

    if not specialite:
        return jsonify({"succes": False, "erreur": "Merci de préciser votre spécialité (ligue/championnat)."}), 400

    # CORRECTIF NOBELLIS : verrou anti-course - sans lui, deux requêtes quasi simultanées
    # (double-clic, script, ou plusieurs threads en production) pourraient toutes deux franchir
    # le contrôle "is_analyst" ci-dessus avant que l'une des deux n'ait eu le temps d'écrire son
    # résultat, créant deux profils analystes dupliqués pour la même personne. Le verrou couvre
    # toute la séquence vérification-puis-action, pas seulement l'écriture finale - c'est
    # précisément là qu'était la fenêtre de course. Protège uniquement à l'intérieur d'un seul
    # processus Python ; un déploiement multi-processus nécessiterait un verrou distribué (Redis,
    # transaction base de données), hors de portée tant que l'app reste en mémoire pure.
    with _VERROU_CREATION_ANALYSTE:
        profil_existant = next((a for a in DATA_ANALYSTES if a["id"] == profil_visiteur.get("analyst_id")), None)

        if profil_existant:
            # --- Réactivation d'un profil existant (jamais de recréation) ---
            date_desactivation_str = profil_existant.get("date_desactivation")
            duree_absence_secondes = 0.0
            if date_desactivation_str:
                date_desactivation = datetime.fromisoformat(date_desactivation_str)
                temps_ecoule = datetime.now(timezone.utc) - date_desactivation
                if temps_ecoule < timedelta(hours=48):
                    heures_restantes = round(48 - temps_ecoule.total_seconds() / 3600, 1)
                    return jsonify({
                        "succes": False,
                        "erreur": f"Réactivation disponible dans {heures_restantes}h (délai de sécurité anti-abus)."
                    }), 403
                duree_absence_secondes = temps_ecoule.total_seconds()

            # CORRECTIF NOBELLIS : cumul de la DURÉE réelle de cette absence (pas juste un compteur
            # d'événements) - base du calcul de présence affiché publiquement, impossible à
            # manipuler en multipliant de courtes absences plutôt qu'une seule longue.
            profil_existant["total_secondes_inactif"] = profil_existant.get("total_secondes_inactif", 0) + duree_absence_secondes

            profil_existant["statut_compte"] = "actif"
            profil_existant["date_desactivation"] = None
            profil_existant["specialite"] = specialite
            profil_existant["nombre_reactivations"] = profil_existant.get("nombre_reactivations", 0) + 1
            # CORRECTIF NOBELLIS : badge_nobellis, pronostics_resolus, pronostics_gagnes, wilson_score,
            # is_following, parrainages ne sont JAMAIS touchés ici - la réputation et les données
            # sociales survivent intégralement à un cycle quitter/revenir.

            profil_visiteur["is_analyst"] = True
            profil_visiteur["specialite"] = specialite

            sauvegarder_profil_visiteur(obtenir_visiteur_id(), profil_visiteur)
            # AJOUT NOBELLIS : synchronisation Firestore après réactivation - ne retarde ni ne fait
            # jamais échouer la réponse déjà prête pour l'utilisateur (voir firestore_client.py).
            _synchroniser_firestore_arriere_plan(data_analystes=DATA_ANALYSTES)

            return jsonify({"succes": True, "analyst_id": profil_existant["id"], "reactivation": True})

        # --- Tout nouveau profil analyste ---
        nouveau_analyst_id = f"exp_{uuid.uuid4().hex[:12]}"  # CORRECTIF NOBELLIS : uuid4, plus de collision possible (contrairement à int(time.time()))
        maintenant_iso = datetime.now(timezone.utc).isoformat()
        DATA_ANALYSTES.append({
            "id": nouveau_analyst_id,
            "nom": profil_visiteur.get("nom", "Nouvel Analyste"),
            "specialite": specialite,
            "precision": 0,
            "total_posts": 0,
            "parrainages": 0,
            "photo_url": profil_visiteur.get("photo_url", "avatars/placeholder.png").replace("/static/", ""),
            "certifie": False,
            "code_promo": "",
            "tendance": "up",
            "is_following": False,
            "liste_pronostics": [],
            "pronostics_resolus": 0,
            "pronostics_gagnes": 0,
            "badge_nobellis": "Nouveau",
            # CORRECTIF NOBELLIS : champs du cycle de statut réversible
            "statut_compte": "actif",
            "date_activation_initiale": maintenant_iso,
            "date_desactivation": None,
            "nombre_reactivations": 0,
            "total_secondes_inactif": 0.0
        })

        profil_visiteur["is_analyst"] = True
        profil_visiteur["analyst_id"] = nouveau_analyst_id
        profil_visiteur["specialite"] = specialite

        sauvegarder_profil_visiteur(obtenir_visiteur_id(), profil_visiteur)
        # AJOUT NOBELLIS : synchronisation Firestore après création d'un nouveau profil analyste.
        _synchroniser_firestore_arriere_plan(data_analystes=DATA_ANALYSTES)

        return jsonify({"succes": True, "analyst_id": nouveau_analyst_id, "reactivation": False})


@app.route('/api/profil/quitter-analyste', methods=['POST'])
def api_quitter_analyste():
    """
    CORRECTIF NOBELLIS : Quitte le statut d'analyste - désactivation, jamais suppression.
    L'historique (badge, Wilson, pronostics, followers) reste intact et continue d'être
    recalculé normalement même hors ligne (recalculer_stats_tous_analystes ne filtre pas
    par statut_compte) - impossible de "geler" sa réputation en se rendant invisible.

    CORRECTIF NOBELLIS : rate-limiting ajouté - même correction que sur devenir-analyste.
    """
    if not verifier_rate_limit(request.remote_addr, max_requetes=5):
        return jsonify({"succes": False, "erreur": "Trop de requêtes."}), 429

    # CORRECTIF NOBELLIS (comptes séparés, Lot 3, 16/09/2026) : profil du visiteur courant -
    # récupéré ICI, avant tout verrou (même règle que partout ailleurs aujourd'hui, voir
    # api_reinitialiser_profil pour le détail du risque d'interblocage sinon).
    profil_visiteur = obtenir_profil_courant()

    if not profil_visiteur.get("is_analyst"):
        return jsonify({"succes": False, "erreur": "Vous n'êtes pas actuellement Analyste."}), 400

    analyste = next((a for a in DATA_ANALYSTES if a["id"] == profil_visiteur.get("analyst_id")), None)
    if not analyste:
        return jsonify({"succes": False, "erreur": "Profil analyste introuvable."}), 404

    with _VERROU_MUTATION_ETAT:
        analyste["statut_compte"] = "inactif"
        analyste["date_desactivation"] = datetime.now(timezone.utc).isoformat()  # Horodatage serveur exclusif
        profil_visiteur["is_analyst"] = False

        sauvegarder_profil_visiteur(obtenir_visiteur_id(), profil_visiteur)
        # AJOUT NOBELLIS : synchronisation Firestore après désactivation du statut analyste.
        _synchroniser_firestore_arriere_plan(data_analystes=DATA_ANALYSTES)

    return jsonify({"succes": True, "message": "Statut d'analyste désactivé. Votre historique est conservé."})


@app.route('/api/profil/reinitialiser', methods=['POST'])
def api_reinitialiser_profil():
    """
    CORRECTIF NOBELLIS (audit, réinitialisation du profil) : remet le profil PERSONNEL du visiteur
    (pseudo, réglages, abonnements, statut analyste) à son état d'origine - JAMAIS l'historique
    public. Si le visiteur est analyste actif, son compte est désactivé exactement comme
    quitter-analyste (statut_compte="inactif") - ses analyses déjà publiées, son score de Wilson,
    ses followers restent intacts et visibles, jamais supprimés. Cette route ne doit être appelée
    qu'après une confirmation explicite côté client (voir app.js) - une réinitialisation ne se
    devine jamais d'un simple clic accidentel.
    """
    if not verifier_rate_limit(request.remote_addr, max_requetes=3):
        return jsonify({"succes": False, "erreur": "Trop de requêtes."}), 429

    # CORRECTIF NOBELLIS (comptes séparés, Lot 2/2, 16/09/2026) : profil du visiteur courant,
    # jamais plus DATA_PROFIL (singulier, partagé). Le bloc figé ci-dessous (analyst_id, username,
    # etc.) a été vérifié MÉCANIQUEMENT identique, champ par champ, à DATA_PROFIL_MODELE_PAR_DEFAUT
    # (déjà construit hier) - remplacé par une deepcopy de ce modèle unique, pour ne plus jamais
    # avoir deux copies du même gabarit qui pourraient diverger silencieusement dans le futur.
    #
    # CORRECTIF NOBELLIS (audit, interblocage évité, 16/09/2026) : obtenir_profil_courant() PREND
    # DÉJÀ _VERROU_MUTATION_ETAT en interne - l'appeler à l'intérieur du "with" ci-dessous aurait
    # provoqué un interblocage total (le thread attend indéfiniment un verrou qu'il tient déjà,
    # threading.Lock() n'étant jamais réentrant). Appelé ICI, avant le verrou, jamais dedans.
    profil_visiteur = obtenir_profil_courant()

    with _VERROU_MUTATION_ETAT:
        if profil_visiteur.get("is_analyst"):
            analyste = next((a for a in DATA_ANALYSTES if a["id"] == profil_visiteur.get("analyst_id")), None)
            if analyste:
                analyste["statut_compte"] = "inactif"
                analyste["date_desactivation"] = datetime.now(timezone.utc).isoformat()

        profil_visiteur.clear()
        profil_visiteur.update(copy.deepcopy(DATA_PROFIL_MODELE_PAR_DEFAUT))
        profil_visiteur["id"] = f"USR-{secrets.token_hex(6).upper()}"  # Comportement d'origine inchangé : toujours un id neuf à la réinitialisation, jamais l'ancien conservé.

        sauvegarder_profil_visiteur(obtenir_visiteur_id(), profil_visiteur)
        _synchroniser_firestore_arriere_plan(data_analystes=DATA_ANALYSTES)

    return jsonify({"succes": True, "message": "Profil réinitialisé avec succès."})



@app.route('/api/admin/diagnostic-profil', methods=['GET'])
def api_diagnostic_profil():
    """
    CORRECTIF NOBELLIS : endpoint de diagnostic protégé - expose l'état RÉEL du profil
    directement depuis la mémoire du processus qui traite cette requête précise. Utile pour
    trancher en quelques secondes entre : (a) l'activation a réellement échoué côté serveur,
    (b) le navigateur affiche une page non rafraîchie, ou (c) plusieurs processus serveur
    (ex: gunicorn --workers > 1) se partagent des états mémoire différents et incohérents -
    dans ce dernier cas, deux appels successifs à cette route peuvent renvoyer des PID
    différents avec des is_analyst différents, ce qui prouve la cause sans ambiguïté.
    Protégé par la même clé admin que les autres routes sensibles - jamais d'exposition publique.
    """
    if not verifier_cle_admin():
        return jsonify({"succes": False, "erreur": "Accès administrateur refusé (clé manquante ou invalide)."}), 403

    # CORRECTIF NOBELLIS (comptes séparés, Lot 2/1, 16/09/2026) : montre désormais le profil du
    # VISITEUR QUI APPELLE cette route (toi, l'admin, la plupart du temps) - plus "LE" profil
    # unique d'avant. Le diagnostic multi-workers (PID incohérent) reste valable, mais la
    # comparaison "is_analyst" entre deux appels n'a de sens que si le même navigateur/cookie
    # est utilisé pour les deux appels - à garder en tête en cas de diagnostic futur.
    profil_visiteur = obtenir_profil_courant()
    analyst_id = profil_visiteur.get("analyst_id")
    analyste_correspondant = next((a for a in DATA_ANALYSTES if a["id"] == analyst_id), None)

    return jsonify({
        "succes": True,
        "pid_processus": os.getpid(),
        "version_serveur": HEURE_DEMARRAGE_SERVEUR,
        "is_analyst": profil_visiteur.get("is_analyst", False),
        "analyst_id": analyst_id,
        "analyst_id_coherent": bool(analyst_id is None or analyste_correspondant is not None),
        "analyste_statut_compte": analyste_correspondant.get("statut_compte") if analyste_correspondant else None,
        "nb_analystes_total": len(DATA_ANALYSTES),
    }), 200


@app.route('/api/admin/sante-donnees', methods=['GET'])
def api_admin_sante_donnees():
    """
    AJOUT NOBELLIS (Chantier 4, tableau de bord de santé des données, 16/09/2026) : expose
    l'état de fraîcheur des données synchronisées, en lisant EXCLUSIVEMENT des valeurs déjà
    en mémoire - aucun nouvel appel API, aucun nouveau risque de quota. Réutilise
    algorithme._classements_sont_frais (seuil 48h) comme SEULE source de vérité sur la
    fraîcheur, plutôt que de dupliquer ce calcul ici avec un risque de désynchronisation future
    entre les deux seuils. Protégé par la même clé admin que les autres routes sensibles.

    CORRECTIF NOBELLIS (accès simplifié, 26/09/2026) : accepte la clé admin en paramètre d'URL
    (?cle=...) EN PLUS de l'en-tête X-Admin-Key habituel - même exception, pour la même raison,
    que celle déjà accordée à /api/admin/mon-visiteur-id : cette route est UNIQUEMENT en
    lecture, sans aucun effet de bord, et existe justement pour être consultable en tapant
    l'adresse directement dans un navigateur. verifier_cle_admin() lui-même reste strict,
    en-tête uniquement, pour toutes les routes qui MODIFIENT quelque chose.
    """
    cle_fournie = request.args.get("cle", "") or request.headers.get("X-Admin-Key", "")
    if not secrets.compare_digest(cle_fournie, ADMIN_SECRET_KEY):
        return jsonify({"succes": False, "erreur": "Accès administrateur refusé (clé manquante ou invalide)."}), 403

    maintenant = datetime.now(timezone.utc)
    prochaine_sync = _prochaine_heure_synchronisation(maintenant)
    matchs_bloques = _lister_matchs_api_bloques(maintenant)

    return jsonify({
        "succes": True,
        "pid_processus": os.getpid(),
        "demarre_depuis": HEURE_DEMARRAGE_SERVEUR,
        "prochaine_synchronisation_utc": prochaine_sync.isoformat(),
        "volumes": {
            "matchs_en_memoire": len(DATA_MATCHS),
            "analystes_en_memoire": len(DATA_ANALYSTES),
            "equipes_avec_classement": max(0, len(DATA_CLASSEMENTS) - 1),  # -1 : exclut la clé de métadonnée.
            "equipes_avec_forme_reelle": max(0, len(DATA_FORME_REELLE) - 1),
            "equipes_avec_stats_historiques": len(DATA_STATS_HISTORIQUES),
        },
        "fraicheur": {
            "classements_derniere_maj_utc": DATA_CLASSEMENTS.get("_meta_derniere_maj_utc"),
            "classements_frais": algorithme._classements_sont_frais(DATA_CLASSEMENTS),
            "forme_reelle_derniere_maj_utc": DATA_FORME_REELLE.get("_meta_derniere_maj_utc"),
            "forme_reelle_fraiche": algorithme._classements_sont_frais(DATA_FORME_REELLE),
        },
        # AJOUT NOBELLIS (24/09/2026) : dernière synchronisation quotidienne réussie, et matchs réels
        # jamais clôturés (à clôturer à la main via /api/admin/matchs/clore/<id>).
        "synchronisation_quotidienne": {
            "derniere_reussie_utc": _ETAT_SYNCHRO_QUOTIDIENNE.get("derniere_reussie_utc"),
        },
        "cloture": {
            "matchs_bloques_nombre": len(matchs_bloques),
            "matchs_bloques": matchs_bloques[:50],
        },
    }), 200


@app.route('/api/admin/mon-visiteur-id', methods=['GET'])
def api_admin_mon_visiteur_id():
    """
    AJOUT NOBELLIS (comptes séparés, Lot 1, 16/09/2026) : révèle l'identifiant de visiteur du
    navigateur qui fait la requête - utile UNE SEULE FOIS, pour la migration manuelle du profil
    existant vers le nouveau système (voir migrer_profil_existant.py).

    CORRECTIF NOBELLIS (accès simplifié, 16/09/2026) : accepte la clé admin en paramètre d'URL
    (?cle=...) EN PLUS de l'en-tête X-Admin-Key habituel - UNIQUEMENT sur cette route précise,
    jamais un changement de verifier_cle_admin() lui-même (qui reste strict, en-tête uniquement,
    pour toutes les routes qui MODIFIENT quelque chose - créer/clôturer un match). Cette route-ci
    est UNIQUEMENT en lecture, sans effet de bord, et existe justement pour permettre de la visiter
    en tapant l'adresse directement dans un navigateur - ce qu'un en-tête personnalisé ne permet
    pas depuis une simple barre d'adresse, contrairement à un paramètre d'URL.
    """
    cle_fournie = request.args.get("cle", "") or request.headers.get("X-Admin-Key", "")
    if not secrets.compare_digest(cle_fournie, ADMIN_SECRET_KEY):
        return jsonify({"succes": False, "erreur": "Accès administrateur refusé (clé manquante ou invalide)."}), 403
    return jsonify({"succes": True, "visiteur_id": obtenir_visiteur_id()}), 200


@app.route('/api/admin/analystes/chercher', methods=['GET'])
def api_admin_chercher_analyste():
    """
    AJOUT NOBELLIS (diagnostic, 26/09/2026) : recherche en lecture seule, par pseudo (sous-chaîne,
    insensible à la casse), pour inspecter les VRAIES fiches d'un analyste - notamment le
    match_id exact que chaque fiche pointe, et si ce match_id existe encore dans DATA_MATCHS
    actuel. Sert à détecter une fiche "orpheline" (match_id qui ne correspond plus à aucun match
    après une resynchronisation) - fiche_est_visible() la considère alors "introuvable" et la
    garde visible par sécurité, indéfiniment. Aucun appel réseau, aucune écriture.
    """
    cle_fournie = request.args.get("cle", "") or request.headers.get("X-Admin-Key", "")
    if not secrets.compare_digest(cle_fournie, ADMIN_SECRET_KEY):
        return jsonify({"succes": False, "erreur": "Accès administrateur refusé (clé manquante ou invalide)."}), 403

    pseudo = request.args.get("pseudo", "").strip().lower()
    if not pseudo:
        return jsonify({"succes": False, "erreur": "Paramètre 'pseudo' manquant (ex: ?pseudo=Nouvel_Utilisateur)."}), 400

    ids_matchs_existants = {m.get("id") for m in DATA_MATCHS}
    resultats = []
    for analyste in DATA_ANALYSTES:
        if pseudo not in str(analyste.get("nom", "")).lower():
            continue
        fiches = []
        for fiche in analyste.get("liste_pronostics") or []:
            match_id = fiche.get("match_id")
            fiches.append({
                "fiche_id": fiche.get("id"),
                "match_id": match_id,
                "match_id_existe_encore": str(match_id) in ids_matchs_existants,
                "publie_le": fiche.get("publie_le"),
                "fiche_visible_maintenant": fiche_est_visible(match_id, fiche.get("publie_le")),
            })
        resultats.append({
            "analyste_id": analyste.get("id"),
            "pseudo": analyste.get("nom"),
            "nb_fiches": len(fiches),
            "fiches": fiches,
        })

    return jsonify({"succes": True, "nb_resultats": len(resultats), "resultats": resultats}), 200


@app.route('/api/admin/matchs/chercher', methods=['GET'])
def api_admin_chercher_matchs():
    """
    AJOUT NOBELLIS (diagnostic, 26/09/2026) : recherche en lecture seule, par nom d'équipe
    (insensible à la casse, sous-chaîne dans "home" OU "away"), pour inspecter les VRAIES
    données stockées d'un match précis (kickoff_utc, statut, cloture_timestamp, id_api...)
    sans dépendre de ce que l'écran affiche - utile pour distinguer un vrai bug de visibilité
    d'un problème de donnée (date illisible, statut inattendu). Aucun appel réseau, aucune
    écriture. Accepte ?cle=... comme /api/admin/sante-donnees et /api/admin/mon-visiteur-id
    (routes en lecture seule uniquement) - jamais pour une route qui modifie une donnée.
    """
    cle_fournie = request.args.get("cle", "") or request.headers.get("X-Admin-Key", "")
    if not secrets.compare_digest(cle_fournie, ADMIN_SECRET_KEY):
        return jsonify({"succes": False, "erreur": "Accès administrateur refusé (clé manquante ou invalide)."}), 403

    nom = request.args.get("nom", "").strip().lower()
    if not nom:
        return jsonify({"succes": False, "erreur": "Paramètre 'nom' manquant (ex: ?nom=Mirassol)."}), 400

    resultats = []
    for match in DATA_MATCHS:
        home = str(match.get("home", ""))
        away = str(match.get("away", ""))
        if nom in home.lower() or nom in away.lower():
            resultats.append({
                "id": match.get("id"),
                "home": home,
                "away": away,
                "competition": match.get("competition"),
                "kickoff_utc": match.get("kickoff_utc"),
                "statut": match.get("statut"),
                "source": match.get("source"),
                "cloture_timestamp": match.get("cloture_timestamp"),
                "score_final": match.get("score_final"),
                "id_api": match.get("id_api"),
                "id_api_football_creation": match.get("id_api_football_creation"),
                "fiche_visible_maintenant": fiche_est_visible(match.get("id")),
            })

    return jsonify({"succes": True, "nb_resultats": len(resultats), "resultats": resultats[:30]}), 200


@app.route('/api/admin/matchs/creer', methods=['POST'])
def api_admin_creer_match():
    """
    CORRECTIF NOBELLIS : Création d'un nouveau match (réservée à l'administrateur).
    Nécessaire pour que le verrou anti-fraude ait un match réel à protéger.
    """
    if not verifier_cle_admin():
        return jsonify({"succes": False, "erreur": "Accès administrateur refusé (clé manquante ou invalide)."}), 403

    data: Dict[str, Any] = request.get_json(silent=True) or {}
    competition = str(data.get("competition", "")).strip()
    home = str(data.get("home", "")).strip()
    away = str(data.get("away", "")).strip()
    kickoff_utc = str(data.get("kickoff_utc", "")).strip()

    if not (competition and home and away and kickoff_utc):
        return jsonify({"succes": False, "erreur": "Compétition, équipes et date de coup d'envoi sont obligatoires."}), 400

    try:
        kickoff_dt = datetime.fromisoformat(kickoff_utc)
        if kickoff_dt <= datetime.now(timezone.utc):
            return jsonify({"succes": False, "erreur": "Le coup d'envoi doit être dans le futur."}), 400
    except ValueError:
        return jsonify({"succes": False, "erreur": "Format de date invalide (attendu : ISO 8601, ex: 2026-09-01T21:00:00+00:00)."}), 400

    with _VERROU_MUTATION_ETAT:
        # CORRECTIF NOBELLIS : uuid4, plus de collision possible (contrairement à int(time.time())) -
        # même correctif que celui déjà appliqué à nouveau_analyst_id, étendu ici pour la même raison :
        # deux matchs créés dans la même seconde produisaient le même id, rendant le second injoignable
        # par recherche d'id (silencieusement, sans aucune erreur).
        nouveau_match_id = f"m_{uuid.uuid4().hex[:12]}"
        DATA_MATCHS.append({
            "id": nouveau_match_id,
            "competition": competition,
            "slug": competition.lower().replace(" ", ""),
            "heure": kickoff_dt.strftime("%H:%M"),
            "home": home,
            "away": away,
            "home_logo_url": str(data.get("home_logo_url", "logos/placeholder.png")),
            "away_logo_url": str(data.get("away_logo_url", "logos/placeholder.png")),
            "tendance": "false",
            "type": "officiel",
            "kickoff_utc": kickoff_utc,
            "statut": "a_venir",
            "score_final": None,
            "cloture_timestamp": None,
            "corrections_score": [],
            # CORRECTIF NOBELLIS (audit, faille n°8) : cette route est le point d'entrée officiel
            # des vrais matchs (admin aujourd'hui, API football demain) - "api" et non "demo", pour
            # déclencher automatiquement le masquage des matchs de test (voir obtenir_matchs_visibles).
            "source": "api"
        })

        # AJOUT NOBELLIS : synchronisation Firestore après création d'un nouveau match.
        _synchroniser_firestore_arriere_plan(data_matchs=DATA_MATCHS)

    return jsonify({"succes": True, "match_id": nouveau_match_id})


@app.route('/api/admin/matchs/a-cloturer', methods=['GET'])
def api_admin_matchs_a_cloturer():
    """
    CORRECTIF NOBELLIS : Liste visible des matchs dépassant 3h après leur coup d'envoi
    sans être clôturés - empêche l'oubli sélectif des matchs aux résultats défavorables.
    """
    if not verifier_cle_admin():
        return jsonify({"succes": False, "erreur": "Accès administrateur refusé (clé manquante ou invalide)."}), 403

    maintenant = datetime.now(timezone.utc)
    resultats = []
    for match in DATA_MATCHS:
        if match.get("statut") != "a_venir":
            continue
        try:
            kickoff_dt = datetime.fromisoformat(str(match.get("kickoff_utc")))
        except (ValueError, TypeError):
            continue
        if maintenant - kickoff_dt > timedelta(hours=3):
            resultats.append({
                "id": match["id"],
                "match_nom": f"{match['home']} vs {match['away']}",
                "kickoff_utc": match.get("kickoff_utc")
            })
    return jsonify({"succes": True, "matchs": resultats})


@app.route('/api/admin/matchs/clore/<match_id>', methods=['POST'])
def api_admin_cloturer_match(match_id: str):
    """
    CORRECTIF NOBELLIS : Clôture d'un match avec score final (réservée à l'administrateur).
    Immuabilité stricte pour les pronostics des analystes, mais correction encadrée et tracée
    du score final admin possible dans une fenêtre de 24h, pour distinguer l'erreur de bonne foi
    de la fraude - qui, elle, reste impossible.
    """
    if not verifier_cle_admin():
        return jsonify({"succes": False, "erreur": "Accès administrateur refusé (clé manquante ou invalide)."}), 403

    match = next((m for m in DATA_MATCHS if m["id"] == str(match_id)), None)
    if not match:
        return jsonify({"succes": False, "erreur": "Match introuvable."}), 404

    data: Dict[str, Any] = request.get_json(silent=True) or {}
    score_final = str(data.get("score_final", "")).strip()
    if not score_final:
        return jsonify({"succes": False, "erreur": "Score final invalide (format attendu : 2-1, ou ANNULE)."}), 400

    with _VERROU_MUTATION_ETAT:
        maintenant = datetime.now(timezone.utc)

        # CORRECTIF NOBELLIS : statut "annulé" pour un match interrompu/reporté/annulé -
        # ne nécessite pas de score numérique, traité avant toute tentative de parsing.
        if score_final.upper() == "ANNULE":
            if match["statut"] == "a_venir":
                match["statut"] = "annule"
                match["score_final"] = "ANNULE"
                match["cloture_timestamp"] = maintenant.isoformat()
                annuler_pronostics_du_match(match["id"])

                # AJOUT NOBELLIS : synchronisation Firestore - annuler_pronostics_du_match() modifie
                # aussi DATA_PREDICTIONS_EXPERT et DATA_ANALYSTES (résultats + stats recalculées).
                _synchroniser_firestore_arriere_plan(
                    data_matchs=DATA_MATCHS,
                    data_predictions_expert=DATA_PREDICTIONS_EXPERT,
                    data_analystes=DATA_ANALYSTES,
                )

                return jsonify({"succes": True, "message": "Match marqué comme annulé. Pronostics exclus des statistiques."})
            return jsonify({"succes": False, "erreur": "Ce match a déjà été clôturé ou annulé."}), 403

        if '-' not in score_final:
            return jsonify({"succes": False, "erreur": "Score final invalide (format attendu : 2-1, ou ANNULE)."}), 400

        try:
            buts_dom_str, buts_ext_str = score_final.split('-')
            int(buts_dom_str.strip()); int(buts_ext_str.strip())
        except ValueError:
            return jsonify({"succes": False, "erreur": "Score final invalide (format attendu : 2-1, ou ANNULE)."}), 400

        if match["statut"] == "a_venir":
            match["statut"] = "termine"
            match["score_final"] = score_final
            match["cloture_timestamp"] = maintenant.isoformat()
            resoudre_pronostics_du_match(match["id"], score_final)

            # AJOUT NOBELLIS : synchronisation Firestore - resoudre_pronostics_du_match() modifie
            # aussi DATA_PREDICTIONS_EXPERT et DATA_ANALYSTES (résultats + stats recalculées).
            _synchroniser_firestore_arriere_plan(
                data_matchs=DATA_MATCHS,
                data_predictions_expert=DATA_PREDICTIONS_EXPERT,
                data_analystes=DATA_ANALYSTES,
            )

            return jsonify({"succes": True, "message": "Match clôturé, statistiques recalculées."})

        # Match déjà clôturé : fenêtre de correction encadrée de 24h, toujours tracée.
        try:
            cloture_dt = datetime.fromisoformat(str(match.get("cloture_timestamp")))
        except (ValueError, TypeError):
            return jsonify({"succes": False, "erreur": "Horodatage de clôture corrompu. Correction bloquée par sécurité."}), 403

        if maintenant - cloture_dt > timedelta(hours=24):
            return jsonify({"succes": False, "erreur": "Fenêtre de correction de 24h dépassée. Score définitif."}), 403

        ancien_score = match["score_final"]
        ancien_statut = match["statut"]
        match["score_final"] = score_final
        # CORRECTIF NOBELLIS : un match "annulé" corrigé avec un vrai score doit repasser "terminé" -
        # sans cette ligne, le statut restait "annule" alors qu'un vrai score et des pronostics
        # gagné/perdu venaient d'être enregistrés dessus (trouvé et prouvé par test avant correction).
        match["statut"] = "termine"
        match.setdefault("corrections_score", []).append({
            "ancien": ancien_score,
            "nouveau": score_final,
            "statut_avant": ancien_statut,
            "corrige_le": maintenant.isoformat()
        })
        resoudre_pronostics_du_match(match["id"], score_final)

        # AJOUT NOBELLIS : synchronisation Firestore après correction d'un score déjà clôturé.
        _synchroniser_firestore_arriere_plan(
            data_matchs=DATA_MATCHS,
            data_predictions_expert=DATA_PREDICTIONS_EXPERT,
            data_analystes=DATA_ANALYSTES,
        )

        return jsonify({"succes": True, "message": "Score corrigé (trace conservée), statistiques recalculées."})


def _parser_seuil(valeur: str):
    """Parse une valeur de type 'OVER_2.5' ou 'UNDER_0.5' en (direction, seuil). None si invalide/vide."""
    if not valeur or "_" not in str(valeur):
        return None
    try:
        direction, seuil_str = str(valeur).split("_", 1)
        if direction not in ("OVER", "UNDER"):
            return None
        return direction, float(seuil_str)
    except (ValueError, IndexError):
        return None


def _bornes_buts(seuil_parse):
    """Retourne (minimum, maximum) de buts compatibles avec un seuil OVER/UNDER (None = pas de borne)."""
    if seuil_parse is None:
        return (0, None)
    direction, seuil = seuil_parse
    if direction == "OVER":
        return (int(seuil) + 1, None)
    return (0, int(seuil))


def _verifier_somme_coherente(seuil_total_str: str, seuil_p1_str: str, seuil_p2_str: str,
                                libelle_p1: str, libelle_p2: str, libelle_total: str, unite: str = "but(s)") -> List[str]:
    """Vérifie qu'un seuil 'total' est mathématiquement compatible avec la somme de deux seuils 'partiels'."""
    seuil_total = _parser_seuil(seuil_total_str)
    if seuil_total is None:
        return []
    seuil_p1 = _parser_seuil(seuil_p1_str)
    seuil_p2 = _parser_seuil(seuil_p2_str)
    if seuil_p1 is None and seuil_p2 is None:
        return []
    min_p1, max_p1 = _bornes_buts(seuil_p1)
    min_p2, max_p2 = _bornes_buts(seuil_p2)
    min_total_implique = min_p1 + min_p2
    max_total_implique = (max_p1 if max_p1 is not None else 999) + (max_p2 if max_p2 is not None else 999)
    direction, seuil = seuil_total

    if direction == "UNDER" and min_total_implique > int(seuil):
        return [
            f"Contradiction : '{libelle_total} : moins de {seuil}' est incompatible avec vos seuils "
            f"'{libelle_p1}' et '{libelle_p2}', qui impliquent déjà au moins {min_total_implique} {unite} au total."
        ]
    if direction == "OVER" and max_total_implique < int(seuil) + 1:
        return [
            f"Contradiction : '{libelle_total} : plus de {seuil}' est incompatible avec vos seuils "
            f"'{libelle_p1}' et '{libelle_p2}', qui limitent le total à {max_total_implique} {unite} maximum."
        ]
    return []


# =========================================================================
# CORRECTIF NOBELLIS (audit, faille n°2) : VALIDATION DE FORMAT DES MARCHÉS
# =========================================================================
# Avant ce correctif, les champs market_* / extended_* étaient acceptés tels quels, sans limite
# de longueur ni de format - un analyste malveillant pouvait envoyer un texte énorme dans un seul
# champ, polluant Firestore et cassant la mise en page. Les listes ci-dessous sont recopiées
# EXACTEMENT (accents, majuscules, parenthèses, signe %) depuis les attributs data-value des
# boutons du formulaire dans app_nobellis.html - toute modification d'un bouton côté template DOIT
# être répercutée ici, sous peine de rejeter en erreur des analystes qui utilisent pourtant
# l'interface officielle sans la modifier. Ce couplage est documenté ici volontairement, pour ne
# pas reproduire une 4e fois le type d'incohérence déjà trouvé sur matchs_publiables/logo_url.
MARCHES_CHOIX_FERME: Dict[str, set] = {
    "market_1n2": {"V1", "X", "V2"},
    "market_dc": {"1X", "12", "X2"},
    "market_btts": {"Oui", "Non"},
    "market_penalty": {"Oui", "Non"},
    "market_rouges": {"Oui", "Non"},
    "market_domination": {"Domicile", "Equilibre", "Exterieur"},
    "market_possession": {"Dominante (-45%)", "Équilibrée (45%-55%)", "Dominante (+55%)"},
}

# Tous les champs à seuil (OVER_x / UNDER_x) du formulaire - le plus grand seuil réellement
# proposé par un bouton est 12.5 (vérifié dans le template), la borne de 15.0 laisse une marge
# raisonnable sans ouvrir la porte à des valeurs aberrantes.
MARCHES_SEUIL: set = {
    "market_goals", "market_cartons",
    "extended_total_v1", "extended_total_v2", "extended_total_m1", "extended_total_m2",
    "extended_total_v1m1", "extended_total_v1m2", "extended_total_v2m1", "extended_total_v2m2",
    "extended_corners_match", "extended_corners_v1", "extended_corners_v2",
    "extended_corners_m1", "extended_corners_m2",
    "extended_corners_v1m1", "extended_corners_v1m2", "extended_corners_v2m1", "extended_corners_v2m2",
    "extended_tirs_cadres", "extended_tirs_v1", "extended_tirs_v2",
    "extended_coups_francs", "extended_coups_v1", "extended_coups_v2",
    "extended_cartons_v1", "extended_cartons_v2",
}

_REGEX_SEUIL_MARCHE = re.compile(r"^(OVER|UNDER)_(\d{1,2}(?:\.\d)?)$")
_REGEX_SCORE_MARCHE = re.compile(r"^\d{1,2}-\d{1,2}$")
SEUIL_MARCHE_MAX = 15.0
LONGUEUR_MAX_SCENARIO = 2000


def valider_format_marches(predictions: Dict[str, Any], scenario: str) -> List[str]:
    """
    Valide le FORMAT de chaque champ (contrairement à valider_coherence_marches, qui valide leur
    COHÉRENCE entre eux). Un champ vide ('') est toujours accepté - tous les marchés sont optionnels,
    seule une valeur non-vide et invalide est rejetée.
    """
    erreurs: List[str] = []

    for nom_champ, valeurs_autorisees in MARCHES_CHOIX_FERME.items():
        valeur = str(predictions.get(nom_champ, "") or "").strip()
        if valeur and valeur not in valeurs_autorisees:
            erreurs.append(f"Valeur invalide pour '{nom_champ}' : ce choix ne correspond à aucune option du formulaire.")

    for nom_champ in MARCHES_SEUIL:
        valeur = str(predictions.get(nom_champ, "") or "").strip()
        if not valeur:
            continue
        correspondance = _REGEX_SEUIL_MARCHE.match(valeur)
        if not correspondance:
            erreurs.append(f"Format invalide pour '{nom_champ}' : seuil attendu du type OVER_2.5 ou UNDER_1.5.")
            continue
        if float(correspondance.group(2)) > SEUIL_MARCHE_MAX:
            erreurs.append(f"Seuil hors limite pour '{nom_champ}' : maximum {SEUIL_MARCHE_MAX}.")

    valeur_score = str(predictions.get("market_score", "") or "").strip()
    if valeur_score and not _REGEX_SCORE_MARCHE.match(valeur_score):
        erreurs.append("Format invalide pour 'market_score' : score exact attendu du type 2-1.")

    if scenario and len(scenario) > LONGUEUR_MAX_SCENARIO:
        erreurs.append(f"Le scénario dépasse la longueur maximale autorisée ({LONGUEUR_MAX_SCENARIO} caractères).")

    return erreurs


# =========================================================================
# CORRECTIF NOBELLIS : VALIDATION DE COHÉRENCE EXHAUSTIVE - TOUS LES MARCHÉS
# =========================================================================
# Empêche un analyste de publier une fiche qui se contredit elle-même (ex: score exact 2-2
# mais BTTS "Non" ; total match "-0.5" mais total équipe domicile "+0.5", mathématiquement
# impossible). Couvre l'intégralité des 32 marchés du formulaire (buts, corners, cartons,
# tirs cadrés, coups francs, répartition par mi-temps). Chaque contradiction détectée génère
# un message qui EXPLIQUE précisément à l'analyste laquelle de ses réponses contredit laquelle -
# pour qu'il comprenne que c'est une erreur de sa part, pas un bug de l'application.
def valider_coherence_marches(predictions: Dict[str, Any]) -> List[str]:
    contradictions: List[str] = []
    p = predictions

    m_1n2 = str(p.get("market_1n2", "")).strip().upper()
    m_dc = str(p.get("market_dc", "")).strip().upper()
    m_btts = str(p.get("market_btts", "")).strip().capitalize()
    m_score = str(p.get("market_score", "")).strip()

    verifications_sommes = [
        ("market_goals", "extended_total_v1", "extended_total_v2", "Total Buts Domicile", "Total Buts Extérieur", "Total Buts au Match", "but(s)"),
        ("market_goals", "extended_total_m1", "extended_total_m2", "Total Buts 1ère MT", "Total Buts 2ème MT", "Total Buts au Match", "but(s)"),
        ("extended_total_v1", "extended_total_v1m1", "extended_total_v1m2", "Buts Domicile 1ère MT", "Buts Domicile 2ème MT", "Total Buts Domicile", "but(s)"),
        ("extended_total_v2", "extended_total_v2m1", "extended_total_v2m2", "Buts Extérieur 1ère MT", "Buts Extérieur 2ème MT", "Total Buts Extérieur", "but(s)"),
        ("extended_total_m1", "extended_total_v1m1", "extended_total_v2m1", "Buts Domicile 1ère MT", "Buts Extérieur 1ère MT", "Total Buts 1ère MT", "but(s)"),
        ("extended_total_m2", "extended_total_v1m2", "extended_total_v2m2", "Buts Domicile 2ème MT", "Buts Extérieur 2ème MT", "Total Buts 2ème MT", "but(s)"),
        ("extended_corners_match", "extended_corners_v1", "extended_corners_v2", "Corners Domicile", "Corners Extérieur", "Total Corners Match", "corner(s)"),
        ("extended_corners_match", "extended_corners_m1", "extended_corners_m2", "Corners 1ère MT", "Corners 2ème MT", "Total Corners Match", "corner(s)"),
        ("extended_corners_v1", "extended_corners_v1m1", "extended_corners_v1m2", "Corners Domicile 1ère MT", "Corners Domicile 2ème MT", "Total Corners Domicile", "corner(s)"),
        ("extended_corners_v2", "extended_corners_v2m1", "extended_corners_v2m2", "Corners Extérieur 1ère MT", "Corners Extérieur 2ème MT", "Total Corners Extérieur", "corner(s)"),
        ("extended_corners_m1", "extended_corners_v1m1", "extended_corners_v2m1", "Corners Domicile 1ère MT", "Corners Extérieur 1ère MT", "Total Corners 1ère MT", "corner(s)"),
        ("extended_corners_m2", "extended_corners_v1m2", "extended_corners_v2m2", "Corners Domicile 2ème MT", "Corners Extérieur 2ème MT", "Total Corners 2ème MT", "corner(s)"),
        ("market_cartons", "extended_cartons_v1", "extended_cartons_v2", "Cartons Domicile", "Cartons Extérieur", "Total Cartons Match", "carton(s)"),
        ("extended_tirs_cadres", "extended_tirs_v1", "extended_tirs_v2", "Tirs Cadrés Domicile", "Tirs Cadrés Extérieur", "Total Tirs Cadrés", "tir(s)"),
        ("extended_coups_francs", "extended_coups_v1", "extended_coups_v2", "Coups Francs Domicile", "Coups Francs Extérieur", "Total Coups Francs", "coup(s) franc(s)"),
    ]
    for cle_total, cle_p1, cle_p2, lib_p1, lib_p2, lib_total, unite in verifications_sommes:
        contradictions.extend(_verifier_somme_coherente(
            p.get(cle_total, ""), p.get(cle_p1, ""), p.get(cle_p2, ""), lib_p1, lib_p2, lib_total, unite
        ))

    if m_score and "-" in m_score:
        try:
            buts_h_str, buts_a_str = m_score.split("-")
            buts_h, buts_a = int(buts_h_str.strip()), int(buts_a_str.strip())

            resultat_reel = "V1" if buts_h > buts_a else ("V2" if buts_a > buts_h else "X")
            if m_1n2 and m_1n2 != resultat_reel:
                libelles = {"V1": "victoire Domicile", "X": "match Nul", "V2": "victoire Extérieur"}
                contradictions.append(
                    f"Contradiction : votre score exact ({m_score}) correspond à une {libelles[resultat_reel]}, "
                    f"mais vous avez sélectionné '{libelles.get(m_1n2, m_1n2)}' dans le marché 1N2."
                )

            dc_valides = {"V1": {"1X", "12"}, "X": {"1X", "X2"}, "V2": {"12", "X2"}}
            if m_dc and m_dc not in dc_valides[resultat_reel]:
                contradictions.append(
                    f"Contradiction : votre score exact ({m_score}) n'est pas couvert par votre sélection "
                    f"'Double Chance : {m_dc}'."
                )

            btts_reel = "Oui" if (buts_h > 0 and buts_a > 0) else "Non"
            if m_btts and m_btts != btts_reel:
                contradictions.append(
                    f"Contradiction : votre score exact ({m_score}) implique que le BTTS (Buts des Deux Équipes) "
                    f"devrait être '{btts_reel}', mais vous avez sélectionné '{m_btts}'."
                )

            seuils_buts_a_verifier = [
                ("market_goals", buts_h + buts_a, "Total Buts au Match", "but(s) au total"),
                ("extended_total_v1", buts_h, "Total Buts Domicile", "but(s) pour l'équipe domicile"),
                ("extended_total_v2", buts_a, "Total Buts Extérieur", "but(s) pour l'équipe extérieure"),
            ]
            for cle_champ, valeur_reelle, libelle, description in seuils_buts_a_verifier:
                seuil_parse = _parser_seuil(p.get(cle_champ, ""))
                if seuil_parse is None:
                    continue
                direction, seuil = seuil_parse
                respecte = (valeur_reelle > seuil) if direction == "OVER" else (valeur_reelle < seuil)
                if not respecte:
                    contradictions.append(
                        f"Contradiction : votre score exact ({m_score}) implique {valeur_reelle} {description}, "
                        f"incompatible avec votre sélection '{libelle} : {'plus' if direction=='OVER' else 'moins'} de {seuil}'."
                    )
        except (ValueError, IndexError):
            pass

    return contradictions


@app.route('/api/analystes/publier', methods=['POST'])
def api_publier_analyse_cumulative():
    """
    Interception, extraction et validation du flux de publication cumulative.
    Intègre un traceur d'audit en temps réel visible dans la console Pydroid 3.
    """
    if not verifier_rate_limit(request.remote_addr, max_requetes=5):
        return jsonify({"succes": False, "erreur": "Flux saturé. Réessayez dans quelques secondes."}), 429

    data: Dict[str, Any] = request.get_json(silent=True) or {}
    match_id = data.get('match_id')

    # CORRECTIF NOBELLIS (comptes séparés, Lot 4, 16/09/2026) : profil du visiteur courant,
    # jamais plus DATA_PROFIL (singulier, partagé). Récupéré ICI, avant tout verrou -
    # obtenir_profil_courant() prend déjà _VERROU_MUTATION_ETAT en interne (voir
    # api_reinitialiser_profil pour le détail du risque d'interblocage sinon).
    profil_visiteur = obtenir_profil_courant()

    # CORRECTIF NOBELLIS : contrôle manquant jusqu'ici - sans lui, "quitter le statut d'analyste"
    # n'empêchait pas réellement de continuer à publier. Garantit que la désactivation a un effet réel.
    if not profil_visiteur.get("is_analyst"):
        return jsonify({"succes": False, "erreur": "Seuls les analystes actifs peuvent publier une analyse."}), 403
    
    # JOURNALISATION D'AUDIT SUR LA CONSOLE DE PYDROID 3 (VÉRIFICATION ENTRÉE PAYLOAD)
    print(f"\n📥 [AUDIT NOBELLIS IA] - Flux de publication reçu pour le Match ID : {match_id}")
    print(f"📦 Contenu Brut JSON du Front-end : {data}\n")
    
    if not match_id:
        return jsonify({"succes": False, "erreur": "Identifiant de confrontation manquant."}), 400

    match_source = next((m for m in DATA_MATCHS if m["id"] == str(match_id)), None)
    if not match_source:
        return jsonify({"succes": False, "erreur": "Match introuvable sur le serveur."}), 404

    # CORRECTIF NOBELLIS : VERROU ANTI-FRAUDE - vérification exclusivement côté serveur.
    # Aucune publication n'est acceptée après le coup d'envoi réel du match (comparaison UTC stricte).
    if match_source.get("statut") == "termine":
        return jsonify({"succes": False, "erreur": "Ce match est déjà terminé. Publication impossible."}), 403

    kickoff_brut = match_source.get("kickoff_utc")
    try:
        kickoff_dt = datetime.fromisoformat(str(kickoff_brut))
        if datetime.now(timezone.utc) >= kickoff_dt:
            return jsonify({"succes": False, "erreur": "Le coup d'envoi de ce match est déjà passé. Publication impossible après le début du match."}), 403
    except (ValueError, TypeError):
        # Fail-safe : configuration horaire invalide -> blocage par sécurité, jamais un passage silencieux.
        return jsonify({"succes": False, "erreur": "Configuration horaire du match invalide. Publication bloquée par sécurité."}), 403

    # CORRECTIF NOBELLIS : VERROU ANTI-FRAUDE - un analyste ne peut plus avoir deux fiches actives
    # (potentiellement contradictoires) sur le même match. Avant, publier deux avis opposés sur un
    # même match garantissait artificiellement une victoire à la clôture, gonflant le volume résolu
    # sans compétence réelle. Ici : toute republication sur un match déjà couvert REMPLACE l'ancienne
    # fiche, dans LES DEUX structures qui la stockent (DATA_PREDICTIONS_EXPERT ET liste_pronostics -
    # deux dictionnaires séparés, non liés par référence, vérifié avant d'écrire ce correctif).
    # L'ancien ID est entièrement retiré des deux côtés avant que le nouveau soit inséré, pour ne
    # jamais laisser une version périmée visible publiquement pendant que le calcul des résultats
    # utiliserait déjà la nouvelle. Autorisé uniquement avant le coup d'envoi (déjà garanti ci-dessus) -
    # un analyste professionnel doit pouvoir ajuster son pronostic suite à une actualité de dernière
    # minute (blessure, composition), ce n'est pas la même chose que parier les deux issues à la fois.
    with _VERROU_MUTATION_ETAT:
        id_expert_courant = profil_visiteur.get("analyst_id")
        ancienne_fiche_meme_match = next(
            (pid for pid, p in DATA_PREDICTIONS_EXPERT.items()
             if p.get("expert_id") == id_expert_courant and p.get("match_id") == str(match_id)),
            None
        )
        if ancienne_fiche_meme_match:
            del DATA_PREDICTIONS_EXPERT[ancienne_fiche_meme_match]
            # CORRECTIF NOBELLIS (audit, mise en avant "non-vu") : retire aussi cette fiche du
            # carnet des fiches déjà vues, si elle y était - sinon ce carnet grossirait pour
            # toujours avec des identifiants pointant vers des fiches qui n'existent plus.
            if ancienne_fiche_meme_match in profil_visiteur.get("fiches_deja_vues", []):
                profil_visiteur["fiches_deja_vues"].remove(ancienne_fiche_meme_match)
            expert_pour_nettoyage = next((a for a in DATA_ANALYSTES if a["id"] == id_expert_courant), None)
            if expert_pour_nettoyage:
                expert_pour_nettoyage["liste_pronostics"] = [
                    f for f in expert_pour_nettoyage.get("liste_pronostics", [])
                    if f.get("match_id") != str(match_id)
                ]

        # CORRECTIF NOBELLIS : uuid4, plus de collision possible (contrairement à int(time.time())) -
        # même correctif que nouveau_analyst_id et nouveau_match_id : deux publications dans la même
        # seconde écrasaient silencieusement l'une l'autre dans DATA_PREDICTIONS_EXPERT (dictionnaire
        # indexé par id), avec un message de succès trompeur pour les deux. Aucune dépendance ailleurs
        # sur le format de cet id - le tri chronologique utilise déjà le champ séparé publie_le.
        nouveau_prono_id = f"p_{uuid.uuid4().hex[:12]}"

        # Extraction et structuration de la matrice des marchés
        predictions_structure = {
            "market_1n2": data.get('market_1n2', ''),
            "market_dc": data.get('market_dc', ''),
            "market_btts": data.get('market_btts', ''),
            "market_domination": data.get('market_domination', ''),
            "market_possession": data.get('market_possession', ''),
            "market_penalty": data.get('market_penalty', ''),
            "market_rouges": data.get('market_rouges', ''),
            "market_score": data.get('market_score', ''),
            "market_goals": data.get('market_goals', ''),
            "extended_total_v1": data.get('extended_total_v1', ''),
            "extended_total_v2": data.get('extended_total_v2', ''),
            "extended_total_m1": data.get('extended_total_m1', ''),
            "extended_total_m2": data.get('extended_total_m2', ''),
            "extended_total_v1m1": data.get('extended_total_v1m1', ''),
            "extended_total_v1m2": data.get('extended_total_v1m2', ''),
            "extended_total_v2m1": data.get('extended_total_v2m1', ''),
            "extended_total_v2m2": data.get('extended_total_v2m2', ''),
            "extended_corners_match": data.get('extended_corners_match', ''),
            "extended_corners_v1": data.get('extended_corners_v1', ''),
            "extended_corners_v2": data.get('extended_corners_v2', ''),
            "extended_corners_m1": data.get('extended_corners_m1', ''),
            "extended_corners_m2": data.get('extended_corners_m2', ''),
            "extended_corners_v1m1": data.get('extended_corners_v1m1', ''),
            "extended_corners_v1m2": data.get('extended_corners_v1m2', ''),
            "extended_corners_v2m1": data.get('extended_corners_v2m1', ''),
            "extended_corners_v2m2": data.get('extended_corners_v2m2', ''),
            "extended_tirs_cadres": data.get('extended_tirs_cadres', ''),
            "extended_tirs_v1": data.get('extended_tirs_v1', ''),
            "extended_tirs_v2": data.get('extended_tirs_v2', ''),
            "extended_coups_francs": data.get('extended_coups_francs', ''),
            "extended_coups_v1": data.get('extended_coups_v1', ''),
            "extended_coups_v2": data.get('extended_coups_v2', ''),
            "market_cartons": data.get('market_cartons', ''),
            "extended_cartons_v1": data.get('extended_cartons_v1', ''),
            "extended_cartons_v2": data.get('extended_cartons_v2', '')
        }

        # Capture et sécurisation du texte de l'argumentation rédigée (déplacé ici, avant les
        # validations, car valider_format_marches en a besoin pour vérifier sa longueur).
        texte_redige = data.get('scenario', '').strip()

        # CORRECTIF NOBELLIS (audit, faille n°2) : rejet de tout champ de marché dont le FORMAT est
        # invalide (valeur hors liste, seuil mal formé, score non conforme, scénario trop long) -
        # AVANT de vérifier leur cohérence entre eux, pour donner à l'analyste l'erreur la plus
        # précise et la plus utile en premier.
        erreurs_format = valider_format_marches(predictions_structure, texte_redige)
        if erreurs_format:
            return jsonify({
                "succes": False,
                "erreur": "Votre analyse contient des champs invalides. Corrigez-les avant de publier.",
                "contradictions": erreurs_format
            }), 400

        # CORRECTIF NOBELLIS : rejet de toute fiche dont les marchés se contredisent entre eux.
        # Chaque contradiction est expliquée en détail pour que l'analyste comprenne qu'il s'agit
        # d'une erreur de saisie de sa part, pas d'un bug de l'application.
        contradictions_detectees = valider_coherence_marches(predictions_structure)
        if contradictions_detectees:
            return jsonify({
                "succes": False,
                "erreur": "Votre analyse contient des contradictions entre marchés. Corrigez-les avant de publier.",
                "contradictions": contradictions_detectees
            }), 400
        
        if not texte_redige or texte_redige == "":
            texte_redige = "Aucune argumentation rédigée pour ce décryptage."

        # CORRECTIF NOBELLIS : Horodatage réel et lisible de la publication - preuve vérifiable
        # que la fiche a bien été soumise avant le coup d'envoi (déjà garanti par le verrou ci-dessus,
        # ici on rend cette preuve consultable par l'utilisateur final, pas seulement appliquée en coulisse).
        horodatage_publication = datetime.now(timezone.utc).isoformat()

        # CORRECTIF NOBELLIS : Consensus réel calculé AVANT insertion de la fiche courante,
        # pour ne pas se comparer à elle-même dans le décompte.
        consensus_calcule = calculer_consensus_reel(str(match_id), predictions_structure.get('market_1n2', ''))

        # Enregistrement de la nouvelle entité d'analyse technique
        DATA_PREDICTIONS_EXPERT[nouveau_prono_id] = {
            "match_id": str(match_id),
            "expert_id": profil_visiteur["analyst_id"],
            "expert_color": "#00e676",
            "match_nom": f"{match_source['home']} vs {match_source['away']}",  # CORRECTIF NOBELLIS : cohérence avec la fiche affichée
            "consensus_pct": consensus_calcule,  # CORRECTIF NOBELLIS : consensus réel, plus de chiffre fixe
            "comment_analyst": texte_redige,  # Fixation propre de la clé de persistance
            "predictions": predictions_structure,
            "publie_le": horodatage_publication
        }

        # Insertion en tête de liste dans l'affichage de l'expert actif
        expert_actif = next((a for a in DATA_ANALYSTES if a["id"] == profil_visiteur["analyst_id"]), None)
        if expert_actif:
            # CORRECTIF NOBELLIS (audit, faille n°1) : plus de normalisation manuelle ici - on stocke
            # home_logo_url/away_logo_url tels quels (chemin local OU URL complète d'une future API
            # football). C'est le filtre Jinja `logo_url` (voir sa définition plus haut) qui décide,
            # au moment de l'affichage, comment les interpréter. Une seule source de vérité : plus
            # aucun risque de divergence entre deux logiques différentes qui traitent le même champ.
            h_logo = match_source["home_logo_url"]
            a_logo = match_source["away_logo_url"]
            
            nouvelle_fiche = {
                "id": nouveau_prono_id,
                "match_id": str(match_id),
                "match_nom": f"{match_source['home']} vs {match_source['away']}",
                "match_home": match_source["home"],
                "match_away": match_source["away"],
                "home_logo_url": h_logo,
                "away_logo_url": a_logo,
                "consensus_pct": consensus_calcule,
                "publie_le": horodatage_publication,
                "resultat": "en_attente"
            }
            expert_actif["liste_pronostics"].insert(0, nouvelle_fiche)
            expert_actif["total_posts"] = len(expert_actif["liste_pronostics"])
            profil_visiteur["total_analyses"] = expert_actif["total_posts"]

        sauvegarder_profil_visiteur(obtenir_visiteur_id(), profil_visiteur)
        # AJOUT NOBELLIS : synchronisation Firestore après publication d'une analyse.
        _synchroniser_firestore_arriere_plan(
            data_analystes=DATA_ANALYSTES,
            data_predictions_expert=DATA_PREDICTIONS_EXPERT,
        )

        return jsonify({"succes": True}), 200
# =========================================================================
# SYSTEME DE SÉCURISATION ET DE SYNCHRONISATION DES PARAMÈTRES AVANCÉS
# =========================================================================
TAILLE_MAX_AVATAR_OCTETS = 2 * 1024 * 1024  # 2 Mo - cohérent avec la limite déjà annoncée côté interface

# CORRECTIF NOBELLIS : signatures binaires ("magic bytes") des formats d'image courants -
# utilisées en repli si Pillow n'est pas disponible (compatibilité Pydroid 3 mobile).
_SIGNATURES_IMAGE = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"), (b"GIF89a", "gif"),
    (b"RIFF", "webp"),  # WEBP : 'RIFF' en tête, 'WEBP' à l'octet 8 - vérifié séparément ci-dessous
)

try:
    from PIL import Image
    import io as _io
    PILLOW_DISPONIBLE = True
except ImportError:
    PILLOW_DISPONIBLE = False


def valider_et_enregistrer_avatar(base64_avatar: str, chemin_destination: str) -> Optional[str]:
    """
    CORRECTIF NOBELLIS : Validation stricte AVANT toute écriture sur disque - ferme la faille
    de déni de service par upload (prouvée par test : 4.8 Mo de contenu non-image acceptés
    sans blocage). Retourne un message d'erreur (str) si invalide, ou None si tout est correct
    et le fichier a été écrit avec succès.
    Utilise Pillow si disponible (validation robuste, décode réellement l'image) ; sinon repli
    sur la vérification des signatures binaires ("magic bytes") en tête de fichier - moins
    exhaustif mais sans dépendance externe, pour rester compatible avec un environnement mobile
    comme Pydroid 3 qui pourrait ne pas avoir Pillow installé.
    """
    if not base64_avatar or "," not in base64_avatar:
        return "Format d'image invalide."

    try:
        _, encoded_data = base64_avatar.split(",", 1)
        image_bytes = base64.b64decode(encoded_data, validate=True)
    except Exception:
        return "Le contenu envoyé n'est pas un base64 valide."

    if len(image_bytes) > TAILLE_MAX_AVATAR_OCTETS:
        taille_mo = round(len(image_bytes) / (1024 * 1024), 2)
        return f"Image trop volumineuse ({taille_mo} Mo) - la limite est de 2 Mo."
    if len(image_bytes) == 0:
        return "Fichier image vide."

    if PILLOW_DISPONIBLE:
        try:
            image = Image.open(_io.BytesIO(image_bytes))
            image.verify()  # Vérifie l'intégrité réelle du fichier image
        except Exception:
            return "Le fichier envoyé n'est pas une image valide ou est corrompu."
    else:
        est_une_image_connue = any(image_bytes.startswith(sig) for sig, _ in _SIGNATURES_IMAGE)
        est_webp_valide = image_bytes.startswith(b"RIFF") and len(image_bytes) > 12 and image_bytes[8:12] == b"WEBP"
        if not est_une_image_connue or (image_bytes.startswith(b"RIFF") and not est_webp_valide):
            return "Le fichier envoyé ne correspond à aucun format d'image reconnu."

    try:
        os.makedirs(os.path.dirname(chemin_destination), exist_ok=True)
        with open(chemin_destination, "wb") as fichier_disque:
            fichier_disque.write(image_bytes)
    except OSError:
        return "Erreur d'écriture sur le serveur. Réessayez."

    return None


@app.route('/api/profil/mettre-a-jour-parametres', methods=['POST'])
def api_mettre_a_jour_parametres():
    """
    Mise à jour unifiée des préférences du Centre de Contrôle.
    Intercepte le payload asynchrone pour modifier instantanément le comportement applicatif.
    """
    if not verifier_rate_limit(request.remote_addr, max_requetes=5):
        return jsonify({"succes": False, "erreur": "Trop de requêtes."}), 429

    data: Dict[str, Any] = request.get_json(silent=True) or {}
    
    # Capture chirurgicale des nouvelles variables envoyées par l'interface ⚙️
    username: str = data.get('username', '').strip()[:30]  # CORRECTIF NOBELLIS : limite de longueur
    base64_avatar: str = data.get('avatar_b64', '').strip()

    # CORRECTIF NOBELLIS : plantage serveur réel corrigé (ValueError non gérée si seuil_chaos
    # n'est pas numérique, prouvé par test). Repli sur 60 si invalide, borné 0-100 dans tous les cas.
    try:
        seuil_chaos: int = int(data.get('seuil_chaos', 60))
    except (ValueError, TypeError):
        seuil_chaos = 60
    seuil_chaos = max(0, min(100, seuil_chaos))

    # CORRECTIF NOBELLIS : validation contre la liste réelle des langues supportées, repli sur 'fr'.
    app_lang: str = data.get('app_lang', 'fr').strip()
    if app_lang not in ("fr", "en", "es"):
        app_lang = "fr"

    # CORRECTIF NOBELLIS : le thème (sombre/clair) était choisi côté client mais jamais transmis
    # ni persisté - DATA_PROFIL["theme"] existait sans jamais être écrit, donc la préférence se
    # perdait à chaque rechargement malgré un message de succès affiché à l'utilisateur.
    # Repli strict sur 'dark' si valeur absente ou invalide (jamais de valeur arbitraire acceptée).
    theme: str = str(data.get('theme', 'dark')).strip()
    if theme not in ("dark", "light"):
        theme = "dark"

    # CORRECTIF NOBELLIS : mode_diagnostic - conversion stricte en booléen réel, jamais une
    # chaîne "false" acceptée telle quelle (qui serait vraie en Python si mal castée). Repli
    # sécurisé sur False si absent ou de type inattendu.
    mode_diagnostic: bool = data.get('mode_diagnostic') is True

    # CORRECTIF NOBELLIS : même filtrage contre la liste fermée que pour l'activation initiale -
    # sinon un analyste déjà actif pourrait contourner la protection en modifiant sa spécialité
    # depuis Paramètres plutôt qu'à l'activation.
    specialite: str = valider_specialite(data.get('specialite', '').strip()[:200])
    code_promo: str = data.get('code_promo', '').strip()[:20]  # CORRECTIF NOBELLIS : limite de longueur

    if not username:
        return jsonify({"succes": False, "erreur": "Le pseudonyme d'affichage ne peut pas être vide."}), 400

    # CORRECTIF NOBELLIS (comptes séparés, Lot 4, 16/09/2026) : profil du visiteur courant,
    # jamais plus DATA_PROFIL (singulier, partagé). Récupéré ICI, avant tout verrou -
    # obtenir_profil_courant() prend déjà _VERROU_MUTATION_ETAT en interne (voir
    # api_reinitialiser_profil pour le détail du risque d'interblocage sinon).
    profil_visiteur = obtenir_profil_courant()

    with _VERROU_MUTATION_ETAT:
        # 1. Hydratation immédiate de la structure du visiteur courant
        profil_visiteur["username"] = username
        profil_visiteur["nom"] = username  # Double ancrage requis pour la variable Jinja2 {{ profil.nom }}
        profil_visiteur["max_incertitude"] = seuil_chaos
        profil_visiteur["seuil_chaos"] = seuil_chaos  # Utilisé en attribut pour l'évaluation dynamique de l'alerte chaos
        profil_visiteur["langue"] = app_lang
        profil_visiteur["theme"] = theme
        # CORRECTIF NOBELLIS (audit, détection automatique du thème système) : dès qu'un
        # utilisateur enregistre ses paramètres au moins une fois, son thème devient un choix
        # manuel définitif - plus jamais concerné par la détection automatique par la suite.
        profil_visiteur["theme_defini_manuellement"] = True
        profil_visiteur["mode_diagnostic"] = mode_diagnostic

        # CORRECTIF NOBELLIS : validation stricte (taille + type réel de fichier) avant toute
        # écriture sur disque - remplace l'ancienne écriture directe non validée.
        # CORRECTIF NOBELLIS (comptes séparés, Lot 4, 16/09/2026) : profil_visiteur["id"] utilisé
        # directement - obtenir_identifiant_profil_sur() (l'ancien accesseur défensif pour
        # DATA_PROFIL) n'est plus nécessaire ici : obtenir_profil_courant() garantit déjà un "id"
        # toujours présent pour ce visiteur, jamais absent.
        nom_fichier = f"avatar_{profil_visiteur['id']}.jpg"
        if base64_avatar:
            chemin_physique = os.path.join(STATIC_DIR, "avatars", nom_fichier)
            erreur_avatar = valider_et_enregistrer_avatar(base64_avatar, chemin_physique)
            if erreur_avatar:
                return jsonify({"succes": False, "erreur": erreur_avatar}), 400
            profil_visiteur["photo_url"] = f"/static/avatars/{nom_fichier}"

        # 3. Synchronisation instantanée des paramètres de l'archive communautaire DATA_ANALYSTES
        # CORRECTIF NOBELLIS : .get() au lieu de l'indexation directe - "analyst_id" absent est un
        # état légitime (utilisateur jamais devenu analyste), pas une erreur à faire planter.
        expert = next((a for a in DATA_ANALYSTES if a["id"] == profil_visiteur.get("analyst_id")), None)
        if expert:
            expert["nom"] = username
            if base64_avatar and "," in base64_avatar:
                expert["photo_url"] = f"avatars/{nom_fichier}"
            if profil_visiteur.get("is_analyst"):
                if specialite:
                    expert["specialite"] = specialite
                    # CORRECTIF NOBELLIS : DATA_PROFIL n'était jamais mis à jour ici, contrairement à
                    # code_promo juste en dessous (même motif, appliqué de façon incohérente) - résultat
                    # prouvé par test : la fiche publique affichait la nouvelle spécialité pendant que
                    # DATA_PROFIL restait bloqué sur l'ancienne, deux sources de vérité désynchronisées.
                    profil_visiteur["specialite"] = specialite
                if code_promo:
                    expert["code_promo"] = code_promo
                    profil_visiteur["code_promo"] = code_promo

        sauvegarder_profil_visiteur(obtenir_visiteur_id(), profil_visiteur)
        # AJOUT NOBELLIS : synchronisation Firestore après mise à jour des paramètres. `expert` peut
        # être None (utilisateur pas encore analyste) - dans ce cas seul le profil (déjà sauvegardé
        # ci-dessus, via la vraie collection multi-visiteurs) a besoin d'être resynchronisé.
        if expert:
            _synchroniser_firestore_arriere_plan(data_analystes=DATA_ANALYSTES)

    return jsonify({"succes": True, "message": "Préférences enregistrées avec succès et synchronisées."}), 200

# =========================================================================
# POINT D'ENTRÉE ET ALLUMAGE DU SERVEUR MULTI-PLATFORMES (PORT 5000)
# =========================================================================
if __name__ == '__main__':
    # CORRECTIF NOBELLIS : debug=True expose le débogueur interactif Werkzeug, qui permet
    # l'exécution de code arbitraire à distance si l'app est accessible publiquement.
    # Désactivé par défaut ; activable explicitement en développement local via
    # la variable d'environnement FLASK_DEBUG=1, jamais en production.
    mode_debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    # CORRECTIF NOBELLIS : filet de sécurité exécuté AVANT app.run() - garantit que toute
    # spécialité de démo non conforme est corrigée en mémoire avant la première requête servie.
    verifier_integrite_specialites_demo()
    verifier_integrite_horodatages_fiches_demo()

    # CORRECTIF NOBELLIS : affichage explicite du PID au démarrage - cette app stocke son état
    # PRINCIPAL en mémoire Python (DATA_PROFIL, DATA_ANALYSTES), avec persistance vers Firestore
    # ajoutée depuis (voir firestore_client.py) - mais reste conçue pour un seul processus.
    # Un déploiement à plusieurs processus (ex: gunicorn --workers 2+) ferait cohabiter des
    # copies mémoire indépendantes, chacune écrivant sur les MÊMES documents Firestore sans
    # coordination : la dernière écriture gagnerait, les autres seraient silencieusement perdues.
    # Ce print rend ce risque vérifiable en un coup d'œil plutôt que supposé : si
    # /api/admin/diagnostic-profil renvoie un PID différent d'un appel à l'autre, la cause est
    # confirmée sans ambiguïté. NE PAS déployer avec plusieurs workers tant que ceci n'est pas
    # résolu par de vraies transactions Firestore (chantier séparé, non traité ici).
    print(f"\n🔎 [NOBELLIS] Processus serveur démarré avec PID={os.getpid()}.", file=sys.stderr)
    print("🔎 [NOBELLIS] Cette app suppose UN SEUL processus (état en mémoire + persistance Firestore).", file=sys.stderr)
    print("🔎 [NOBELLIS] En cas de doute sur un déploiement multi-workers, comparez le PID retourné", file=sys.stderr)
    print("🔎 [NOBELLIS] par /api/admin/diagnostic-profil entre deux requêtes successives.\n", file=sys.stderr)

    # CORRECTIF NOBELLIS (production, 16/09/2026) : remplace app.run() (serveur de développement
    # Flask, une seule requête traitée à la fois par défaut) par Waitress - pur Python, tourne
    # identiquement sur Pydroid 3 (Android)/Windows/Linux/Mac, multi-thread par défaut. UN SEUL
    # PROCESSUS (jamais plusieurs workers) - voir l'avertissement PID ci-dessus : cette app
    # suppose un état en mémoire partagé, Waitress avec plusieurs threads dans un seul processus
    # respecte cette contrainte, contrairement à un déploiement multi-workers.
    # mode_debug (FLASK_DEBUG=1) garde le débogueur Werkzeug pour le développement local actif -
    # Waitress n'a pas d'équivalent et est réservé à l'usage normal/production.
    if mode_debug:
        print("🔄 [NOBELLIS] Démarrage en mode développement (débogueur Werkzeug actif)...", file=sys.stderr)
        app.run(host='0.0.0.0', port=5000, debug=True)
    else:
        print("🔄 [NOBELLIS] Démarrage du serveur de production Waitress (8 threads, 1 processus)...", file=sys.stderr)
        waitress.serve(app, host='0.0.0.0', port=5000, threads=8)
