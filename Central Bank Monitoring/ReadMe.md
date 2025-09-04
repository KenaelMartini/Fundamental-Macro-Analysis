# Central Bank Monitoring (CBM)

**Central Bank Monitoring (CBM)** est une application desktop développée en **Python/Tkinter** destinée à suivre en temps réel :  

- Les communications et publications des **banques centrales** (Fed, BoE, ECB, BoJ, BoC, RBA, RBNZ, SNB).  
- Le **calendrier macroéconomique** (TradingEconomics, avec fallback HTML en cas de restriction API).  
- Une **analyse automatique** des publications (classification, labelling, biais hawkish/dovish).  

---

## 🎯 Objectif

CBM vise à fournir un outil robuste permettant de :  
- Suivre la politique monétaire en temps réel (banques centrales G10).  
- Anticiper les mouvements de marché via l’intégration directe du calendrier économique.  
- Centraliser et historiser les événements clés (JSONL, CSV, export).  
- Proposer un affichage simple et rapide, adapté à un environnement de **trading desk**.  

---

## ✨ Fonctionnalités principales

- **Dashboard**
  - Heartbeats en temps réel (25 ms pour les banques centrales, ~800 ms pour TE).  
  - Monitoring permanent de la latence et de l’état des watchers.  

- **News**
  - Flux en direct des publications macro (CPI, GDP, PMI, Unemployment, JOLTS, NFP, Retail Sales, etc.).  
  - Catégorisation automatique : Inflation, Growth, Labor Market, PMI, Retail.  
  - Mapping complet : country, currency, event, actual, consensus, previous, importance.  
  - Analyse instantanée (labels + impact potentiel).  

- **Data / Historique / Logs**
  - Archivage systématique en JSONL.  
  - Recherche, filtres, export CSV/JSON pour exploitation externe.  

- **UI Tkinter**
  - Onglets : Dashboard, News, Data, Historique, Logs.  
  - Commandes : Start/Stop/Restart des watchers, Nettoyage, Export.  

---

## 🧑‍💻 Développement

Le projet a été conçu comme un **outil de desk** :  
- Structure modulaire (watchers indépendants + interface unifiée).  
- Robustesse : fallback HTML si l’API est bloquée, gestion d’erreurs, cache local.  
- Extensibilité : ajout progressif de nouvelles banques centrales et nouvelles catégories d’événements macro.  

### Utilisation de l’IA
Dans la phase de développement, j’ai utilisé l’IA :  
- Comme **support de relecture et de vérification** du code.  
- Pour accélérer l’exploration de solutions techniques (threading, parsing HTML, UI Tkinter).  
- Comme **outil d’apprentissage** sur certaines parties spécifiques.  

L’IA a servi de **catalyseur pédagogique**, mais l’architecture, les choix et l’implémentation finale restent le fruit d’un travail personnel.  
