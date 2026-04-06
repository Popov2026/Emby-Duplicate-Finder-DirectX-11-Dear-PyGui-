# Emby-Duplicate-Finder-DirectX-11-Dear-PyGui-

Emby Duplicate Finder (DirectX 11 / Dear PyGui)
A lightweight and high-performance utility designed to identify duplicates in your Emby libraries. This tool features a modern graphical interface powered by the DirectX 11 rendering engine.

🤖 Project Origins
This software is a co-creation between a human developer and Claude AI. This collaboration combined specific user needs (managing complex media libraries) with a robust software architecture and a fluid, multi-threaded user interface.

🛡️ Privacy & Security (Local-First)
This script was built with a "Privacy-First" mindset:

Read-Only: The script analyzes your files and database but never deletes, moves, or modifies your media. You retain 100% control.

100% Local: No data is sent to external servers. The script does not require internet access to function. All processing happens exclusively on your machine.

Transparency: Settings and results are saved locally in .ini and .json files within the script's folder.

✨ Features
Ultra-Fluid Interface: Built with Dear PyGui, offering a highly responsive UI through hardware acceleration (GPU).

Smart Scan: Uses a similarity-based naming algorithm to detect various versions of the same content (4K vs 1080p, Director's Cut, etc.).

Direct Exploration: Open the specific Windows folder containing a duplicate directly from the UI for manual verification.

Persistence: Automatically saves your Server IP, API Key, and library paths for future sessions.

📋 Prerequisites
Before running the script, ensure you have the following:

Python 3.7+ (if running the .pyw script directly).

Dependencies: pip install dearpygui.

Emby API Key (Required): * Log in to your Emby Server dashboard.

Navigate to Settings > Advanced > API Keys.

Generate a new key (e.g., name it "Duplicate Finder").

Copy this key; you will need to enter it into the script's configuration.

System: Windows (required for DirectX rendering and File Explorer integration).

🚀 Setup & Usage
Download the .pyw file (or the compiled executable).

In the Configuration tab of the application:

Enter your Server IP and your API Key.

Set up your library paths.

Settings are automatically saved to a local .ini file.

Launch the scan and manage your duplicates with peace of mind.

Developed with passion to simplify Emby server management.

# Emby-Duplicate-Finder-DirectX-11-Dear-PyGui-

Un utilitaire léger et performant conçu pour identifier les doublons dans vos bibliothèques Emby, développé avec une interface graphique moderne utilisant le moteur de rendu DirectX 11.
🤖 Genèse du projet
Ce logiciel est le fruit d'une co-création entre un utilisateur humain et Claude AI. Cette collaboration a permis d'allier des besoins métier spécifiques à une interface utilisateur fluide basée sur le threading.

🛡️ Confidentialité et Sécurité (Local-First)
Lecture seule : Le script analyse vos fichiers et votre base de données, mais ne supprime, ne déplace et ne modifie aucun de vos médias.

100% Local : Aucune donnée n'est envoyée vers un serveur externe. Le script n'a pas besoin d'accès Internet pour fonctionner.

Transparence : Les réglages et résultats sont sauvegardés localement (fichiers .ini et .json).

✨ Caractéristiques
Interface ultra-fluide : Développé avec Dear PyGui, utilisant l'accélération matérielle (GPU).

Scan intelligent : Algorithme de comparaison basé sur la similarité des noms de fichiers.

Exploration directe : Ouverture du dossier Windows contenant le doublon pour vérification manuelle.

Persistance : Sauvegarde automatique de l'IP du NAS, de la clé API et des chemins.

📋 Prérequis
Avant de lancer le script, assurez-vous d'avoir :

Python 3.7+ (si vous lancez le script .pyw directement).

Dépendances : pip install dearpygui.

Clé API Emby (Obligatoire) : * Connectez-vous à votre tableau de bord Emby Server.

Allez dans Paramètres > Avancé > Clé d'API.

Générez une nouvelle clé (nommez-la "Duplicate Finder" par exemple).

Copiez cette clé, elle vous sera demandée à l'ouverture du script.

Système : Windows (pour le rendu DirectX et l'explorateur).

🚀 Configuration & Utilisation
Téléchargez le fichier .pyw (ou l'exécutable).

Dans l'onglet Configuration du script :

Saisissez l'IP de votre serveur et votre Clé API.

Configurez les chemins de vos bibliothèques.

Les réglages seront automatiquement sauvegardés dans un fichier .ini local pour vos prochaines utilisations.

Lancez le scan et gérez vos doublons en toute sérénité.

Développé avec passion pour simplifier la gestion des serveurs Emby.
Popov2026
