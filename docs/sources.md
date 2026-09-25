# Sources et cours — livrable de l'étape 0

> Spike réalisé le 25/09/2026. Toutes les observations ci-dessous proviennent d'appels réels faits ce jour-là.
> Les réponses brutes sont enregistrées dans `tests/data/sources/` pour les tests des fetchers (étape 3).

## Synthèse

| Agent | Source | Type | Primaire | Statut | Motif |
|---|---|---|---|---|---|
| NANO | ClinicalTrials.gov | `clinicaltrials` | oui | `enabled: true` | API v2 stable, sans authentification |
| NANO | SEC EDGAR | `edgar` | oui | `enabled: true` | Couvre tous les communiqués Nanobiotix (6-K, exhibit 99.1), en même temps que la diffusion presse |
| NANO, UBI | AMF informations réglementées | `dila_amf` | oui | `enabled: true` | API info-financiere.gouv.fr filtrable par ISIN, à jour en quelques minutes |
| NANO, UBI | Google News FR / EN | `google_news_rss` | non | `enabled: true` | Fonctionne, mais l'opérateur `when:` est obligatoire (voir plus bas) |
| UBI | Ubisoft Investor Center | `rss` | oui | `enabled: false` | Pas de flux ; **redondant** : tous les communiqués Ubisoft passent par l'AMF |
| NANO | Nanobiotix IR | `rss` | oui | `enabled: false` | Flux WordPress `nanobiotix.com/feed/` vide ; `ir.nanobiotix.com` renvoie 403 ; **redondant** avec EDGAR et l'AMF |
| NANO | Johnson & Johnson communiqués | `rss` | oui | `enabled: false` | Pas de flux ; `jnj.com` bloque les robots (page Akamai « Site Maintenance ») ; voir la justification plus bas |

Cours : `yfinance` validé avec une réserve (séance manquante), repli identifié : **CSV Euronext**.

Aucune source ne nécessite `html_list` : ce fetcher n'a pas besoin d'être implémenté pour le MVP.

---

## 1. ClinicalTrials.gov (`clinicaltrials`)

- **URL** : `GET https://clinicaltrials.gov/api/v2/studies/{nct_id}` (JSON, ~44 Ko).
- **Authentification** : aucune. La limite publiée par ClinicalTrials.gov est d'environ 50 requêtes par minute et par IP ; un appel par jour suffit.
- **Champs à inclure dans l'instantané** (chemins dans le JSON) :
  - `protocolSection.statusModule.overallStatus` : `RECRUITING` au 25/09/2026 ;
  - `protocolSection.statusModule.primaryCompletionDateStruct` : `2028-06-30`, `ESTIMATED` ;
  - `protocolSection.statusModule.completionDateStruct` : `2028-06-30`, `ESTIMATED` ;
  - `protocolSection.statusModule.lastUpdatePostDateStruct.date` : `2026-08-28` ;
  - `hasResults` (racine) : `false` ;
  - `protocolSection.outcomesModule.primaryOutcomes` : une entrée, *« Progression-free Survival (PFS) Based on Independent Central Review (ICR) »*.
- **Fréquence de mise à jour** : irrégulière. Dernière mise à jour le 28/08/2026, `statusVerifiedDate` : 2026-08.
- **Vérifications faites** : le promoteur est `Johnson & Johnson Enterprise Innovation Inc.`. Les identifiants secondaires sont `NANORAY-312` et `2024-520386-31-00` (numéro EUCT). Le **critère principal est bien la PFS**, ce qui lève le « à confirmer dans le registre » de la thèse.
- **À surveiller** : la date de fin principale estimée (30/06/2028) est **postérieure** à la date butoir `N-T1` (31/12/2027). Soit le registre n'a pas encore intégré l'amendement de mai 2026 (analyse finale avancée), soit le time stop tombera avant les résultats. C'est à l'utilisateur de trancher.

## 2. SEC EDGAR (`edgar`)

- **Liste des dépôts** : `GET https://data.sec.gov/submissions/CIK{cik sur 10 chiffres}.json`, soit `CIK0001760854.json` pour Nanobiotix. Le bloc `filings.recent` est en colonnes : `form`, `accessionNumber`, `filingDate`, `acceptanceDateTime`, `primaryDocument`, `items`, `isInlineXBRL`, etc. Il contient 308 dépôts, dont 230 formulaires 6-K.
- **Documents d'un dépôt** : `GET https://www.sec.gov/Archives/edgar/data/{cik}/{accession sans tirets}/index.json`. L'exhibit s'appelle `exh_991.htm` dans les dépôts via GlobeNewswire (accession `0001171843-…`), mais le nom varie selon l'agent de dépôt : chercher le motif `ex[h]?[-_]?99` dans le nom de fichier.
- **Authentification** : aucune, mais un **`User-Agent` avec un contact est obligatoire** (politique SEC ; variable `SEC_USER_AGENT`). Limite : 10 requêtes par seconde.
- **Couverture** (12 derniers 6-K comparés au flux AMF et à Google News) : résultats semestriels, résultats de phase 1 dans le poumon, conférences, droits de vote mensuels, levée de fonds de mai 2026 (« Closing of Global Offering », 86,1 M€). Le 6-K du 24/09/2026 a été accepté à 20:15:03Z, c'est-à-dire en même temps que la diffusion GlobeNewswire. **EDGAR remplace bien la page investisseurs.**
- **Pièges** :
  - Certains 6-K sont des rapports financiers en inline XBRL sans exhibit 99 (ex. `nbx-20260630.htm`, 1,7 Mo, accession `0001760854-…`). Pour ces dépôts, ne pas envoyer le document principal au LLM. Le même contenu arrive par un communiqué distinct (exhibit 99.1).
  - Le texte de l'exhibit commence par `EX-99.1 … EXHIBIT 99.1` suivi du titre : c'est là qu'il faut extraire `title`.
  - Les publications mensuelles de droits de vote sont aussi déposées en 6-K : ce sont des documents parasites pour le tri (voir §7.4 du cadrage).
- **ID** : le numéro d'accession (`accessionNumber`), stable.

## 3. AMF — informations réglementées (`dila_amf`)

Deux canaux existent. Le cadrage envisageait le premier ; **le second est retenu**.

### 3.1 Archives FTP DILA (non retenu)

- `https://echanges.dila.gouv.fr/OPENDATA/AMF/` : environ 8 archives `amf-gz_JJMMAAAA_HHMM.tar.gz` par jour (07:00 → 23:00), de quelques centaines de Ko chacune.
- Chaque archive contient `xml/<diffuseur>/AAAA/MM/MD*.xml` (métadonnées : `ISO_CD_ISI`, `INF_TIT_INF`, `INF_STP_PRI`, `INF_DAT_EMT`) et `pdf/…/FC*.pdf` (contenu).
- Le schéma XSD et les notices sont dans `0 Presentation et documentation flux AMF/`.
- Inconvénient : il faut télécharger toutes les archives du jour puis filtrer par ISIN.

### 3.2 API info-financiere.gouv.fr (retenu)

- **URL** : `GET https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets/flux-amf-new-prod/records` (Opendatasoft Explore v2.1, JSON).
- **Paramètres utilisés** :
  - `where=identificationsociete_iso_cd_isi="<ISIN>" and informationdeposee_inf_dat_emt>=date'<AAAA-MM-JJ>'`
  - `order_by=informationdeposee_inf_dat_emt desc`
  - `limit` : 100 au maximum par page.
- **Champs utiles** :
  - `uin_idt_uin` : identifiant stable, à utiliser comme ID ;
  - `informationdeposee_inf_dat_emt` : date d'émission, en UTC ;
  - `informationdeposee_inf_tit_inf` : titre ;
  - `type_d_information` / `sous_type_d_information` : catégorie en clair ;
  - `identificationdiffuseur_idi_cod_dif` : `MKW` pour un communiqué de l'émetteur, `307` pour une publication de l'AMF (franchissements de seuil, DEU) ;
  - `url_de_recuperation` : PDF du document.
- **Authentification** : aucune. Un quota anonyme Opendatasoft existe mais n'a pas été mesuré ; le besoin est d'une requête par agent et par jour.
- **Fraîcheur** : le communiqué semestriel Nanobiotix diffusé le 24/09/2026 à 20:15Z était présent le 25/09 au matin. Le rapport financier semestriel a été déposé à 20:26Z.
- **Couverture observée depuis le 01/01/2026** :
  - UBI, 116 documents :
    - 70 franchissements de seuil (AMF) ;
    - 19 informations privilégiées, soit **tous** les communiqués Ubisoft (chiffre d'affaires, résultats, nominations, opérations Guillemot Brothers), en FR et en EN ;
    - 8 publications mensuelles de droits de vote ;
    - documents d'AG et DEU.
  - NANO : résultats, informations privilégiées (phase 1 poumon), franchissements de seuil, droits de vote.
- **Texte** : les PDF s'extraient correctement avec `pypdf`, qui émet des avertissements « Ignoring wrong pointing object » à faire taire dans les logs.
- **Pièges** :
  - Des dates aberrantes existent dans le dataset (ex. `5015-02-04`). **Toujours filtrer** par ISIN et par plage de dates, sans se fier au dernier enregistrement global.
  - Les communiqués sont publiés deux fois, en FR et en EN, avec deux ID différents. Le LLM verra deux documents pour le même événement, ce qui est acceptable puisque les deux preuves sont primaires.
  - Les franchissements de seuil sont nombreux : 5 déclarations pour UBI entre le 21 et le 22/09/2026, dont JP Morgan qui passe 10 % du capital le 17/09/2026. Ils sont indispensables pour U-B3 et U-B5 et ne doivent donc pas être filtrés. Le tri devra les traiter, ce qui représente un coût LLM modéré.
  - Option d'optimisation, non implémentée : un paramètre `exclude_subtypes` permettrait d'écarter `Total du nombre de droits de vote et du capital`, qui ne sert qu'à la mise à jour manuelle de `shares_outstanding`.
- **Paramètres de config** : `isin` uniquement. L'URL de l'API est une constante du fetcher. L'ancien paramètre `base_url` (FTP) a été retiré des configs.

## 4. Google News RSS (`google_news_rss`)

- **URL** : `GET https://news.google.com/rss/search?q=<requête>&hl=<hl>&gl=<gl>&ceid=<ceid>`. Combinaisons testées : `fr / FR / FR:fr` et `en-US / US / US:en`.
- **Opérateurs validés** : `OR`, les guillemets, les parenthèses et `when:Nd`.
- **Point bloquant corrigé par le fetcher** : le flux renvoie au plus **100 résultats triés par pertinence**, pas par date. Sans `when:`, les articles récents sont perdus :

  | Requête | Articles de moins de 3 jours sans `when:` | Avec `when:3d` |
  |---|---|---|
  | NANO FR | 13 | 13 (1 manquant sans `when:`) |
  | UBI FR (édition US) | 1 | 34 |
  | UBI EN (édition US) | 1 | 9 |

  → le fetcher **ajoute toujours** ` when:{max_item_age_days}d` à la requête de la config.
- **Format des entrées** : `id` (guid stable, à utiliser comme ID), `title` (suffixé par « - <éditeur> »), `published` (RFC 822, GMT), `source.title` et `link`.
  - `summary` ne contient **qu'un lien HTML reprenant le titre**, pas de résumé. Le tri travaille donc sur le titre et l'éditeur.
- **Liens** : `link` pointe vers `news.google.com/rss/articles/<id>`, qui redirige vers une page de consentement Google (UE), pas vers l'article.
  - Pour obtenir l'URL de l'éditeur : charger `news.google.com/articles/<id>` avec le cookie `SOCS=CAI`, lire les attributs `data-n-a-sg` et `data-n-a-ts`, puis appeler `POST news.google.com/_/DotsSplashUi/data/batchexecute` (RPC `Fbv4je`). Test réussi sur 5 articles sur 5.
  - Cette API est **interne et non documentée**, donc fragile. `fetch_text` doit fonctionner en « meilleur effort » : si le décodage ou le téléchargement de l'article échoue (paywall, anti-bot), il se replie sur le titre et l'éditeur. Comme la source n'est pas primaire, cela n'affecte jamais une recommandation actionnable.
- **Authentification** : aucune. Pas de limite publiée : rester à quelques requêtes par run.

## 5. Johnson & Johnson (désactivée)

- `jnj.com` (salle de presse, `/rss`, `/feed`) renvoie une page anti-bot « Site Maintenance ». `investor.jnj.com` n'expose pas de flux (404).
- EDGAR J&J (CIK 200406) existe, mais NBTXR3 n'y fait pas l'objet de dépôts propres, comme l'indiquait déjà la spec.
- **Justification** : tout événement J&J important pour la thèse (résultats de NANORAY-312, résiliation de la licence, paiement d'étape) constitue une information privilégiée pour Nanobiotix, qui doit la publier elle-même. Il sera donc capté par EDGAR (6-K) et l'AMF. Les requêtes Google News incluent en plus `"JNJ-1900"` et `NBTXR3`.

## 6. Cours

### 6.1 yfinance (MVP)

- Testé avec `yf.Ticker("UBI.PA" | "NANO.PA").history(period="1mo")` : 23 séances, index en `Europe/Paris`.
- **Problèmes constatés** :
  1. **Séance manquante** : la clôture du **24/09/2026** est absente pour les deux lignes, alors qu'Euronext la publie (UBI : 5,40). Une variation J-1 calculée sur une telle série couvre en réalité deux séances.
  2. **Barre de la séance en cours** : appelée à 10:30, l'API renvoie une barre datée du jour avec un volume partiel. Le provider doit **écarter toute barre datée d'aujourd'hui** (à 07:00 le marché n'est pas ouvert, mais la règle doit être explicite).
- yfinance dépend de `curl_cffi`, qui embarque son propre magasin de certificats (voir la note TLS plus bas).

### 6.2 Repli : CSV Euronext

- **URL** : `GET https://live.euronext.com/en/ajax/AwlHistoricalPrice/getFullDownloadAjax/{ISIN}-XPAR?format=csv&decimal_separator=.&date_form=d/m/Y&op=&adjusted=Y&base100=&startdate=AAAA-MM-JJ&enddate=AAAA-MM-JJ`.
- **Format** : CSV séparé par `;`, avec un BOM et 3 lignes d'en-tête (`"Historical Data"`, période, ISIN). Colonnes : `Date;Open;High;Low;Last;Close;"Number of Shares";"Number of Trades";Turnover;vwap`. Lignes triées par date **décroissante**.
- **Authentification** : aucune. L'endpoint n'est pas documenté officiellement mais les données viennent de la bourse elle-même, et la série est complète (le 24/09 y figure).
- **Impact sur l'interface** : l'endpoint prend l'**ISIN + MIC** (`XPAR`), pas le symbole Yahoo. Le repli doit donc recevoir `position.isin`. Soit `PriceProvider.history` prend la `Position`, soit chaque provider dérive son identifiant de la position. À trancher à l'étape 3.
- **Recommandation** : vu le trou de données Yahoo, envisager Euronext comme provider **principal** pour les lignes Euronext Paris et garder yfinance en repli. Le cadrage ne fixe que « yfinance en MVP, derrière une interface » ; le changement reste compatible, mais la décision revient à l'utilisateur.

### 6.3 Autres pistes écartées

- Stooq : page anti-bot (preuve de travail en JavaScript) sur l'export CSV.
- Boursorama `GetTicksEOD` : HTTP 410.
- Euronext `getHistoricalPriceData` (POST) : page 404.

## 7. Données de position mises à jour

Relevées dans les publications mensuelles AMF (« nombre total de droits de vote et d'actions ») :

| Agent | `shares_outstanding` | `as_of` | Source |
|---|---|---|---|
| UBI | 136 237 168 (inchangé) | 2026-08-31 (était `null`) | Publication du 10/09/2026 |
| NANO | 50 941 528 (était 50 807 903) | 2026-08-31 (était 2026-05-31) | Publication du 22/09/2026, FR et EN |

## 8. Note d'environnement : TLS en local (poste Windows)

L'antivirus Avast (« Web/Mail Shield ») intercepte le TLS sur le poste de développement et présente son propre certificat racine. Le `curl` système l'accepte, car il lit le magasin Windows. En revanche, `httpx`/`certifi` et `curl_cffi` (yfinance) échouent avec `CERTIFICATE_VERIFY_FAILED`.

Pour les essais en local, deux solutions :
- exclure le dossier du projet ou `python.exe` de l'analyse HTTPS d'Avast ;
- ou construire un bundle CA à partir de certifi et du magasin Windows (`ssl.enum_certificates`), puis exporter `SSL_CERT_FILE` et `CURL_CA_BUNDLE`.

Ce problème **n'existe pas** sur le VPS ni dans le conteneur. Il ne faut rien coder en dur dans le projet pour le contourner.

## 9. Points ouverts pour l'utilisateur

1. **Date butoir N-T1** (31/12/2027) antérieure à la fin principale estimée de NANORAY-312 dans le registre (30/06/2028) : voir §1.
2. **Provider de cours principal** : yfinance ou Euronext (§6.2).
3. **Seuils dans `_defaults.yaml`** : `rumor.promote_if_abs_move_pct: 20` et `price_anomaly.abs_move_pct: 10` sont l'inverse de la §2 du cadrage (promotion des rumeurs à 10 %, anomalie à 20 %).
