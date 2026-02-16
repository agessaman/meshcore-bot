#!/usr/bin/env python3
"""
Generate Command Reference Website
Creates a single-page HTML website with bot introduction and command reference
"""

import configparser
import sqlite3
import os
import html
import logging
import sys
import argparse
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from collections import defaultdict

# Import bot modules with error handling
try:
    # Add project root to path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    
    from modules.plugin_loader import PluginLoader
    from modules.db_manager import DBManager
    from modules.utils import resolve_path
    from modules.config_validation import strip_optional_quotes
except ImportError as e:
    print("Error: Missing required dependencies.")
    print(f"Details: {e}")
    print("\nPlease install bot dependencies by running:")
    print("  pip install -r requirements.txt")
    print("\nOr install the missing module directly:")
    if 'pytz' in str(e):
        print("  pip install pytz")
    sys.exit(1)


class MinimalBot:
    """Minimal bot mock for plugin loading without full bot initialization"""
    
    def __init__(self, config, logger, db_manager=None):
        self.config = config
        self.logger = logger
        self.db_manager = db_manager
        
        # Dummy translator
        class DummyTranslator:
            def translate(self, key, **kwargs):
                return key
            def get_value(self, key):
                return None
        
        self.translator = DummyTranslator()
        self.command_manager = None  # Will be set after plugin loading


# Style definitions for website themes
# Each style contains CSS custom property values that will be injected into the :root selector
STYLES = {
    'default': {
        'name': 'Modern Dark',
        'description': 'Dark theme with gradients and modern cards (current default)',
        'fonts_url': 'https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap',
        'css_vars': """--bg-primary: #0a0e14;
            --bg-secondary: #111820;
            --bg-card: #151c25;
            --bg-card-hover: #1a232e;
            --accent-blue: #00d4ff;
            --accent-cyan: #00ffc8;
            --accent-orange: #ff8a00;
            --accent-purple: #a855f7;
            --accent-red: #ff4757;
            --accent-yellow: #ffd700;
            --text-primary: #e8edf4;
            --text-secondary: #8892a4;
            --text-muted: #6b7280;
            --border-subtle: rgba(255,255,255,0.06);
            --glow-blue: rgba(0, 212, 255, 0.15);
            --glow-cyan: rgba(0, 255, 200, 0.1);"""
    },
    'minimalist': {
        'name': 'Minimalist Clean',
        'description': 'Light theme with clean typography and whitespace',
        'fonts_url': 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap',
        'css_vars': """--bg-primary: #ffffff;
            --bg-secondary: #ffffff;
            --bg-card: #ffffff;
            --bg-card-hover: #f8f9fa;
            --accent-blue: #0052cc;
            --accent-cyan: #006699;
            --accent-orange: #d65d0e;
            --accent-purple: #6b21a8;
            --accent-red: #b91c1c;
            --accent-yellow: #a16207;
            --text-primary: #1a1a1a;
            --text-secondary: #4a4a4a;
            --text-muted: #6b6b6b;
            --border-subtle: rgba(0,0,0,0.15);
            --glow-blue: rgba(0,82,204,0.1);
            --glow-cyan: rgba(0,102,153,0.08);""",
        'css_overrides': """
        /* Pure white background - remove atmospheric effects */
        .atmosphere,
        .grid-overlay {
            display: none !important;
        }

        body {
            background: #ffffff !important;
        }

        /* Ultra-minimal: square everything */
        *,
        *::before,
        *::after,
        .container,
        .intro-section,
        .sidebar-nav,
        .sidebar-nav a,
        .command-card,
        .command-keyword,
        .command-usage,
        .channel-section,
        .channel-card,
        button,
        input {
            border-radius: 0 !important;
        }

        /* Clean Inter font everywhere */
        body,
        h1, h2, h3, h4, h5, h6,
        p, a, span, div {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
        }

        /* Remove all shadows */
        .command-card,
        .intro-section,
        .sidebar-nav,
        .command-keyword,
        .command-usage {
            box-shadow: none !important;
        }

        /* Thin, clean borders */
        .command-card {
            border: 1px solid var(--border-subtle) !important;
        }

        .intro-section {
            border: 1px solid var(--border-subtle) !important;
        }

        .sidebar-nav {
            border-right: 1px solid var(--border-subtle) !important;
        }

        .command-keyword {
            border: 1px solid currentColor !important;
            font-weight: 500;
        }

        .command-usage {
            border-left: 2px solid var(--border-subtle) !important;
        }

        /* Minimal hover effects - just background color */
        .command-card:hover {
            box-shadow: none !important;
            transform: none !important;
        }

        .sidebar-nav a:hover {
            background: var(--bg-card-hover) !important;
        }

        /* Typography: lighter weight, more spacing */
        h1 {
            font-weight: 600;
            letter-spacing: -0.02em;
        }

        h2, h3 {
            font-weight: 500;
            letter-spacing: -0.01em;
        }

        .command-name {
            font-weight: 600;
        }

        p, .command-description {
            font-weight: 400;
            line-height: 1.6;
        }

        /* Remove all animations and transitions for instant feedback */
        *, *::before, *::after {
            animation: none !important;
            transition: background-color 0.1s ease !important;
        }

        /* Clean spacing */
        .command-card {
            padding: 1.5rem !important;
        }

        /* Subtle, clean keyword badges */
        .command-keyword {
            padding: 0.25rem 0.5rem;
            font-size: 0.875rem;
        }
        """
    },
    'terminal': {
        'name': 'Terminal/Hacker',
        'description': 'Green/amber on black, monospace, retro terminal aesthetic',
        'fonts_url': 'https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap',
        'css_vars': """--bg-primary: #000000;
            --bg-secondary: #0a0a0a;
            --bg-card: #0f0f0f;
            --bg-card-hover: #1a1a1a;
            --accent-blue: #00ff00;
            --accent-cyan: #00ff00;
            --accent-orange: #ffb000;
            --accent-purple: #00ff00;
            --accent-red: #ff0000;
            --accent-yellow: #ffff00;
            --text-primary: #00ff00;
            --text-secondary: #00aa00;
            --text-muted: #008800;
            --border-subtle: rgba(0,255,0,0.3);
            --glow-blue: rgba(0,255,0,0.2);
            --glow-cyan: rgba(0,255,0,0.15);""",
        'css_overrides': """
        /* Terminal monospace font everywhere */
        body,
        h1, h2, h3, h4, h5, h6,
        p, a, span, div,
        .command-name,
        .command-description,
        .command-usage,
        .command-keyword,
        .sidebar-nav a {
            font-family: 'JetBrains Mono', 'Courier New', monospace !important;
        }

        /* CRT scanline effect */
        body::before {
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: repeating-linear-gradient(
                0deg,
                rgba(0, 255, 0, 0.03),
                rgba(0, 255, 0, 0.03) 1px,
                transparent 1px,
                transparent 2px
            );
            pointer-events: none;
            z-index: 9999;
        }

        /* Terminal text glow */
        h1, h2, h3,
        .command-name {
            text-shadow: 0 0 10px rgba(0, 255, 0, 0.8);
        }

        .command-keyword,
        .command-usage {
            text-shadow: 0 0 5px rgba(0, 255, 0, 0.6);
        }

        /* Cursor blink effect after usage example */
        .command-card:hover .command-usage::after {
            content: '▮';
            display: inline-block;
            margin-left: 0.5rem;
            animation: blink 1s step-end infinite;
            color: var(--accent-blue);
        }

        @keyframes blink {
            0%, 50% { opacity: 1; }
            51%, 100% { opacity: 0; }
        }

        /* Terminal prompt style for usage */
        .command-usage::before {
            content: '$ ';
            color: var(--accent-yellow);
            font-weight: bold;
        }
        """
    },
    'glass': {
        'name': 'Glass/Glassmorphism',
        'description': 'Frosted glass cards with blur effects and colorful gradients',
        'fonts_url': 'https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap',
        'css_vars': """--bg-primary: linear-gradient(135deg, #4c5fd7 0%, #764ba2 100%);
            --bg-secondary: rgba(255,255,255,0.05);
            --bg-card: rgba(255,255,255,0.1);
            --bg-card-hover: rgba(255,255,255,0.15);
            --accent-blue: #a8daff;
            --accent-cyan: #a8fff4;
            --accent-orange: #ffc085;
            --accent-purple: #d4b3ff;
            --accent-red: #ff9eb5;
            --accent-yellow: #ffe8a8;
            --text-primary: #ffffff;
            --text-secondary: rgba(255,255,255,0.8);
            --text-muted: rgba(255,255,255,0.6);
            --border-subtle: rgba(255,255,255,0.2);
            --glow-blue: rgba(168,218,255,0.2);
            --glow-cyan: rgba(168,255,244,0.15);"""
    },
    'neon': {
        'name': 'Neon/Cyberpunk',
        'description': 'Bright neon colors, dark backgrounds, futuristic aesthetic',
        'fonts_url': 'https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700&display=swap',
        'css_vars': """--bg-primary: #0a0014;
            --bg-secondary: #150028;
            --bg-card: #1a0033;
            --bg-card-hover: #25004d;
            --accent-blue: #00f5ff;
            --accent-cyan: #00f5ff;
            --accent-orange: #ff69b4;
            --accent-purple: #bf40bf;
            --accent-red: #ff1493;
            --accent-yellow: #ffd700;
            --text-primary: #ffffff;
            --text-secondary: #e9d5ff;
            --text-muted: #d8b4fe;
            --border-subtle: rgba(157,78,221,0.3);
            --glow-blue: rgba(0,245,255,0.4);
            --glow-cyan: rgba(247,37,133,0.3);""",
        'css_overrides': """
        /* Neon cyberpunk font */
        body,
        h1, h2, h3, h4, h5, h6,
        .command-name,
        .sidebar-nav a {
            font-family: 'Orbitron', sans-serif !important;
            font-weight: 600;
        }

        /* Intense neon glow on headings */
        h1 {
            text-shadow:
                0 0 10px var(--accent-cyan),
                0 0 20px var(--accent-cyan),
                0 0 30px var(--accent-cyan),
                0 0 40px var(--accent-blue);
            letter-spacing: 0.1em;
            text-transform: uppercase;
        }

        h2, h3 {
            text-shadow:
                0 0 10px var(--accent-purple),
                0 0 20px var(--accent-purple);
            letter-spacing: 0.05em;
        }

        .command-name {
            text-shadow:
                0 0 10px var(--accent-cyan),
                0 0 20px var(--accent-cyan);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        /* Neon border glow on cards - subtle */
        .command-card {
            border: 2px solid var(--accent-cyan);
            box-shadow:
                0 0 5px var(--glow-blue),
                inset 0 0 5px rgba(0,245,255,0.05);
        }

        .command-card:hover {
            border-color: var(--accent-orange);
            box-shadow:
                0 0 10px var(--accent-cyan),
                0 0 20px var(--accent-orange),
                inset 0 0 10px rgba(255,105,180,0.1);
            transform: translateY(-4px);
        }

        .intro-section {
            border: 2px solid var(--accent-purple);
            box-shadow:
                0 0 8px var(--accent-purple),
                inset 0 0 8px rgba(191,64,191,0.05);
        }

        /* Neon keyword badges */
        .command-keyword {
            text-shadow: 0 0 8px currentColor;
            border: 1px solid currentColor;
            box-shadow:
                0 0 5px currentColor,
                inset 0 0 5px currentColor;
        }

        /* Glowing usage examples */
        .command-usage {
            border-left: 3px solid var(--accent-cyan);
            box-shadow: -3px 0 10px var(--glow-blue);
            text-shadow: 0 0 5px rgba(0,245,255,0.5);
        }

        /* Animated neon flicker effect */
        @keyframes neon-flicker {
            0%, 100% { opacity: 1; }
            41%, 43% { opacity: 0.8; }
            45%, 47% { opacity: 0.9; }
            49% { opacity: 0.85; }
        }

        h1, .command-name {
            animation: neon-flicker 3s infinite;
        }

        /* Cyberpunk grid overlay */
        body::after {
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-image:
                linear-gradient(rgba(0,245,255,0.03) 1px, transparent 1px),
                linear-gradient(90deg, rgba(0,245,255,0.03) 1px, transparent 1px);
            background-size: 50px 50px;
            pointer-events: none;
            z-index: 1;
        }

        /* Ensure content is above grid */
        .container {
            position: relative;
            z-index: 2;
        }

        /* Sidebar neon accent */
        .sidebar-nav {
            border-right: 2px solid var(--accent-cyan);
            box-shadow: 2px 0 15px var(--glow-blue);
        }

        .sidebar-nav a:hover {
            text-shadow: 0 0 10px var(--accent-cyan);
            background: rgba(0,245,255,0.1);
        }
        """
    },
    'brutalist': {
        'name': 'Brutalist/Bold',
        'description': 'High contrast, bold typography, thick borders, geometric',
        'fonts_url': 'https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700;900&display=swap',
        'css_vars': """--bg-primary: #ffffff;
            --bg-secondary: #f0f0f0;
            --bg-card: #ffffff;
            --bg-card-hover: #f5f5f5;
            --accent-blue: #0000cc;
            --accent-cyan: #006666;
            --accent-orange: #cc5200;
            --accent-purple: #5c00b8;
            --accent-red: #cc0000;
            --accent-yellow: #997700;
            --text-primary: #000000;
            --text-secondary: #1a1a1a;
            --text-muted: #4d4d4d;
            --border-subtle: rgba(0,0,0,1);
            --glow-blue: rgba(0,0,204,0.2);
            --glow-cyan: rgba(0,102,102,0.2);""",
        'css_overrides': """
        /* BRUTALIST: Square everything aggressively */
        *,
        *::before,
        *::after {
            border-radius: 0 !important;
        }

        /* Space Grotesk everywhere, heavy weights */
        body,
        h1, h2, h3, h4, h5, h6,
        p, a, span, div {
            font-family: 'Space Grotesk', sans-serif !important;
        }

        /* MASSIVE, BOLD headings */
        h1 {
            font-weight: 900 !important;
            font-size: 4rem !important;
            letter-spacing: -0.05em;
            text-transform: uppercase;
            line-height: 0.9;
        }

        .category-title {
            font-weight: 900 !important;
            font-size: 2.5rem !important;
            text-transform: uppercase;
            letter-spacing: -0.03em;
            border-bottom: 8px solid #000000 !important;
            padding-bottom: 0.5rem !important;
        }

        .category-title .anchor-link {
            text-decoration: none;
            color: #000000;
        }

        .command-name {
            font-weight: 900 !important;
            font-size: 1.5rem !important;
            text-transform: uppercase;
            letter-spacing: -0.02em;
        }

        /* THICK borders everywhere */
        .command-card {
            border: 6px solid #000000 !important;
            box-shadow: 12px 12px 0 #000000 !important;
        }

        .command-card:hover {
            box-shadow: 16px 16px 0 #000000 !important;
            transform: translate(-4px, -4px) !important;
        }

        /* Brutal intro box */
        .header-content {
            border: 6px solid #000000 !important;
            box-shadow: 12px 12px 0 #000000 !important;
            border-radius: 0 !important;
            background: #ffffff !important;
        }

        .header-content::before {
            display: none !important;
        }

        header {
            border-radius: 0 !important;
        }

        /* Bold, chunky keyword badges */
        .command-keyword {
            font-weight: 700 !important;
            border: 3px solid #000000 !important;
            padding: 0.5rem 1rem !important;
            background: #ffffff;
            text-transform: uppercase;
            font-size: 0.75rem !important;
            letter-spacing: 0.05em;
        }

        .command-keyword:nth-child(odd) {
            background: #000000;
            color: #ffffff;
        }

        /* Heavy usage box */
        .command-usage {
            border: 4px solid #000000 !important;
            background: #f0f0f0 !important;
            padding: 1rem !important;
            font-weight: 700;
        }

        /* Aggressive sidebar - SQUARE */
        .sidebar-nav {
            border-right: 8px solid #000000 !important;
            border-radius: 0 !important;
        }

        .sidebar-nav a {
            font-weight: 700 !important;
            text-transform: uppercase;
            font-size: 0.85rem !important;
            letter-spacing: 0.02em;
            border-radius: 0 !important;
        }

        .sidebar-nav a:hover {
            background: #000000 !important;
            color: #ffffff !important;
        }

        /* Remove blue hover bar on command cards */
        .command-card::before {
            display: none !important;
        }

        /* CHANNEL CARDS - same brutal treatment */
        .channel-card {
            border: 6px solid #000000 !important;
            box-shadow: 12px 12px 0 #000000 !important;
            border-radius: 0 !important;
        }

        .channel-card:hover {
            box-shadow: 16px 16px 0 #000000 !important;
            transform: translate(-4px, -4px) !important;
        }

        .channel-name {
            font-weight: 900 !important;
            font-size: 1.5rem !important;
            text-transform: uppercase;
            letter-spacing: -0.02em;
        }

        .channel-description {
            font-weight: 500 !important;
        }

        .channel-section h2 {
            font-weight: 900 !important;
            font-size: 2.5rem !important;
            text-transform: uppercase;
            letter-spacing: -0.03em;
            border-bottom: 8px solid #000000 !important;
            padding-bottom: 0.5rem !important;
        }

        /* Remove all smooth transitions - instant feedback */
        * {
            transition: none !important;
        }

        /* Heavy typography throughout */
        p, .command-description {
            font-weight: 500 !important;
            line-height: 1.5;
        }

        /* Brutal intro section */
        .intro {
            font-weight: 700 !important;
            font-size: 1.2rem !important;
        }
        """
    },
    'gradient': {
        'name': 'Gradient/Modern',
        'description': 'Colorful gradients, smooth transitions, vibrant colors',
        'fonts_url': 'https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap',
        'css_vars': """--bg-primary: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%);
            --bg-secondary: rgba(0,0,0,0.08);
            --bg-card: rgba(255,255,255,0.95);
            --bg-card-hover: rgba(255,255,255,0.98);
            --accent-blue: #4338ca;
            --accent-cyan: #0891b2;
            --accent-orange: #ea580c;
            --accent-purple: #7c3aed;
            --accent-red: #dc2626;
            --accent-yellow: #ca8a04;
            --text-primary: #1a1a1a;
            --text-secondary: #374151;
            --text-muted: #6b7280;
            --border-subtle: rgba(0,0,0,0.15);
            --glow-blue: rgba(79,70,229,0.3);
            --glow-cyan: rgba(8,145,178,0.25);""",
        'css_overrides': """
        /* Vibrant gradient borders */
        .command-card {
            border: 2px solid transparent;
            background:
                linear-gradient(white, white) padding-box,
                linear-gradient(135deg, #667eea, #764ba2, #f093fb) border-box;
            box-shadow: 0 4px 20px rgba(102, 126, 234, 0.15);
            background-origin: border-box;
            background-clip: padding-box, border-box;
        }

        .command-card:hover {
            box-shadow: 0 8px 30px rgba(102, 126, 234, 0.25), 0 0 0 1px rgba(102, 126, 234, 0.1);
            transform: translateY(-4px);
        }

        .intro-section {
            border: 2px solid transparent;
            background:
                linear-gradient(white, white) padding-box,
                linear-gradient(135deg, #667eea, #00d4ff) border-box;
            box-shadow: 0 4px 20px rgba(102, 126, 234, 0.15);
        }

        /* Gradient text for headings */
        h1 {
            background: linear-gradient(135deg, #667eea, #764ba2, #f093fb);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        /* Keep section headers (category titles) visible with high contrast */
        .category-title .anchor-link {
            background: linear-gradient(135deg, #1a1a1a, #374151);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            text-decoration: none;
            font-weight: 700;
        }

        .command-name {
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            font-weight: 700;
        }

        /* Colorful keyword badges with gradients */
        .command-keyword {
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            border: none;
            font-weight: 600;
        }

        .command-keyword:nth-child(2) {
            background: linear-gradient(135deg, #f093fb, #667eea);
        }

        .command-keyword:nth-child(3) {
            background: linear-gradient(135deg, #764ba2, #f093fb);
        }

        .command-keyword:nth-child(4) {
            background: linear-gradient(135deg, #00d4ff, #667eea);
        }

        .command-keyword:nth-child(5) {
            background: linear-gradient(135deg, #f093fb, #00d4ff);
        }

        /* Animated gradient on hover */
        @keyframes gradient-shift {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }

        .command-card:hover .command-name {
            background: linear-gradient(135deg, #667eea, #764ba2, #f093fb, #00d4ff);
            background-size: 300% 300%;
            animation: gradient-shift 3s ease infinite;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        /* Gradient usage box */
        .command-usage {
            background: linear-gradient(135deg, rgba(102, 126, 234, 0.05), rgba(240, 147, 251, 0.05));
            border-left: 3px solid;
            border-image: linear-gradient(135deg, #667eea, #f093fb) 1;
        }

        /* Sidebar gradient accent */
        .sidebar-nav {
            border-right: 3px solid transparent;
            border-image: linear-gradient(180deg, #667eea, #764ba2, #f093fb) 1;
        }

        .sidebar-nav a:hover {
            background: linear-gradient(90deg, rgba(102, 126, 234, 0.1), transparent);
            border-left: 3px solid #667eea;
            padding-left: 1.5rem;
        }
        """
    },
    'pixel': {
        'name': 'Pixel/Retro',
        'description': 'Pixel art aesthetic, squared boxes, bright retro gaming colors',
        'fonts_url': 'https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap',
        'css_vars': """--bg-primary: #2b2d42;
            --bg-secondary: #3a3d5c;
            --bg-card: #4a4e69;
            --bg-card-hover: #5a5f7e;
            --accent-blue: #00b4d8;
            --accent-cyan: #00f5d4;
            --accent-orange: #ff6b35;
            --accent-purple: #b185db;
            --accent-red: #ef476f;
            --accent-yellow: #ffd60a;
            --text-primary: #ffffff;
            --text-secondary: #e0e0e0;
            --text-muted: #a8a8a8;
            --border-subtle: rgba(255,255,255,0.3);
            --glow-blue: rgba(0,180,216,0.4);
            --glow-cyan: rgba(0,245,212,0.3);""",
        'css_overrides': """
        /* Pixel art: remove ALL rounded corners */
        *,
        *::before,
        *::after,
        .container,
        .intro-section,
        .sidebar-nav,
        .sidebar-nav a,
        .command-card,
        .command-keyword,
        .command-usage,
        .channel-section,
        .channel-card,
        .mobile-menu-toggle,
        button,
        input {
            border-radius: 0 !important;
        }

        /* Chunky pixel borders */
        .command-card {
            border: 3px solid var(--border-subtle) !important;
        }

        .intro-section {
            border: 3px solid var(--border-subtle) !important;
        }

        .sidebar-nav {
            border: 3px solid var(--border-subtle) !important;
        }

        .command-keyword {
            border: 2px solid currentColor !important;
        }

        .command-usage {
            border: 2px solid var(--accent-cyan) !important;
        }

        .channel-section {
            border: 3px solid var(--border-subtle) !important;
        }

        /* Pixel font sizing adjustments */
        body {
            font-family: 'Press Start 2P', cursive !important;
            line-height: 1.8;
        }

        h1 {
            font-size: 1.5rem !important;
            line-height: 1.6;
        }

        h2 {
            font-size: 1.2rem !important;
            line-height: 1.6;
        }

        h3 {
            font-size: 1rem !important;
            line-height: 1.6;
        }

        .command-name {
            font-size: 1.1rem !important;
        }

        p, .command-description, .command-usage {
            font-size: 0.75rem !important;
            line-height: 1.8;
        }

        .command-keyword {
            font-size: 0.65rem !important;
            padding: 4px 8px;
        }

        .sidebar-nav a {
            font-size: 0.7rem !important;
        }

        /* Retro box shadow - hard edges */
        .command-card {
            box-shadow: 4px 4px 0 var(--border-subtle) !important;
        }

        .command-card:hover {
            box-shadow: 8px 8px 0 var(--accent-cyan) !important;
            transform: translate(-2px, -2px) !important;
        }

        .intro-section {
            box-shadow: 4px 4px 0 var(--border-subtle) !important;
        }

        /* Remove smooth transitions for pixel feel */
        * {
            transition: none !important;
        }
        """
    }
}


def setup_logging():
    """Setup basic logging"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s'
    )
    return logging.getLogger(__name__)


def read_config(config_file: str = "config.ini") -> configparser.ConfigParser:
    """Read and parse config.ini file"""
    config = configparser.ConfigParser()
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Config file not found: {config_file}")
    config.read(config_file)
    return config


def get_bot_name(config: configparser.ConfigParser) -> str:
    """Extract bot name from config"""
    return config.get('Bot', 'bot_name', fallback='MeshCore Bot')


def get_admin_commands(config: configparser.ConfigParser) -> List[str]:
    """Extract admin commands from config"""
    if not config.has_section('Admin_ACL'):
        return []
    admin_commands_str = config.get('Admin_ACL', 'admin_commands', fallback='')
    if not admin_commands_str:
        return []
    return [cmd.strip() for cmd in admin_commands_str.split(',') if cmd.strip()]


def get_website_intro(config: configparser.ConfigParser) -> str:
    """Get custom introduction text from config, or use default"""
    if config.has_section('Website'):
        intro = config.get('Website', 'introduction_text', fallback='')
        if intro:
            return intro
    
    # Default introduction (first person from bot's perspective)
    bot_name = get_bot_name(config)
    return f"Hi, I'm {bot_name}! I provide various commands to help you interact with the mesh network. Use the commands below to get started."


def get_website_title(config: configparser.ConfigParser) -> str:
    """Get website title from config, or use default"""
    if config.has_section('Website'):
        title = config.get('Website', 'website_title', fallback='')
        if title:
            return title
    
    bot_name = get_bot_name(config)
    return f"{bot_name} - Command Reference"


def load_channels_from_config(config: configparser.ConfigParser) -> Dict[str, Dict[str, str]]:
    """Load channels from Channels_List section, grouped by category
    
    Returns:
        Dict with structure: {
            'general': {'#channel': 'description', ...},
            'category': {'#channel': 'description', ...},
            ...
        }
    """
    channels = {'general': {}}
    
    if not config.has_section('Channels_List'):
        return channels
    
    for key, description in config.items('Channels_List'):
        key = key.strip()
        description = description.strip()
        
        if not key or not description:
            continue
        
        # Check if it's a categorized channel (has dot notation)
        if '.' in key:
            category = key.split('.')[0]
            channel_name = key.split('.', 1)[1]
            
            if category not in channels:
                channels[category] = {}
            
            # Add # prefix if not present
            display_name = channel_name if channel_name.startswith('#') else f"#{channel_name}"
            channels[category][display_name] = description
        else:
            # General channel (no category)
            display_name = key if key.startswith('#') else f"#{key}"
            channels['general'][display_name] = description
    
    # Remove empty categories
    return {cat: chans for cat, chans in channels.items() if chans}


def get_database_path(config: configparser.ConfigParser, bot_root: str) -> Optional[str]:
    """Get database path from config"""
    db_path = config.get('Bot', 'db_path', fallback='meshcore_bot.db')
    if db_path:
        return resolve_path(db_path, bot_root)
    return None


def get_command_popularity(db_path: Optional[str], commands: Dict[str, Any]) -> Dict[str, int]:
    """Get command usage counts from database"""
    popularity = defaultdict(int)
    
    if not db_path or not os.path.exists(db_path):
        return popularity
    
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # Check if command_stats table exists
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='command_stats'
            """)
            if not cursor.fetchone():
                return popularity
            
            # Query command usage
            cursor.execute("""
                SELECT command_name, COUNT(*) as count 
                FROM command_stats 
                GROUP BY command_name
            """)
            
            for row in cursor.fetchall():
                command_name = row[0]
                count = row[1]
                
                # Map to primary command name if it's a keyword/alias
                primary_name = command_name
                for cmd_name, cmd_instance in commands.items():
                    if hasattr(cmd_instance, 'keywords') and command_name.lower() in [k.lower() for k in cmd_instance.keywords]:
                        primary_name = cmd_instance.name if hasattr(cmd_instance, 'name') else cmd_name
                        break
                    if hasattr(cmd_instance, 'name') and cmd_instance.name == command_name:
                        primary_name = cmd_instance.name
                        break
                
                popularity[primary_name] += count
    except Exception as e:
        logging.warning(f"Could not query command popularity: {e}")
    
    return popularity


def filter_commands(commands: Dict[str, Any], admin_commands: List[str]) -> Dict[str, Any]:
    """Filter out admin and hidden commands"""
    filtered = {}
    
    # Categories to exclude from public reference
    excluded_categories = {'hidden', 'admin', 'system', 'management', 'special'}
    
    for cmd_name, cmd_instance in commands.items():
        # Skip commands in excluded categories
        if hasattr(cmd_instance, 'category') and cmd_instance.category in excluded_categories:
            continue
        
        # Get primary command name
        primary_name = cmd_instance.name if hasattr(cmd_instance, 'name') else cmd_name
        
        # Skip admin commands by name (check both dict key and primary name)
        if cmd_name in admin_commands or primary_name in admin_commands:
            continue
        
        # Skip commands that require admin access
        if hasattr(cmd_instance, 'requires_admin_access') and cmd_instance.requires_admin_access():
            continue
        
        # Skip commands with no keywords (automatic/system commands)
        if hasattr(cmd_instance, 'keywords') and not cmd_instance.keywords:
            continue
        
        filtered[cmd_name] = cmd_instance
    
    return filtered


def get_default_command_order() -> List[str]:
    """Get default command ordering when no stats available"""
    return [
        'help', 'ping', 'test',  # Basic
        'wx', 'aqi',  # Weather
        'sun', 'moon', 'solar',  # Solar
        'sports', 'stats',  # Popular features
    ]


def sort_commands_by_popularity(commands: Dict[str, Any], popularity: Dict[str, int]) -> List[Tuple[str, Any]]:
    """Sort commands by popularity, with fallback to default order"""
    default_order = get_default_command_order()
    
    # Create list of (name, instance, priority) tuples
    command_list = []
    for cmd_name, cmd_instance in commands.items():
        primary_name = cmd_instance.name if hasattr(cmd_instance, 'name') else cmd_name
        
        # Get popularity count
        count = popularity.get(primary_name, 0)
        
        # Get default priority (lower is better)
        try:
            default_priority = default_order.index(primary_name)
        except ValueError:
            default_priority = 999  # Not in default order
        
        # Priority: popularity count (descending), then default order, then alphabetical
        priority = (-count, default_priority, primary_name.lower())
        command_list.append((priority, cmd_name, cmd_instance))
    
    # Sort by priority
    command_list.sort(key=lambda x: x[0])
    
    # Return (name, instance) tuples
    return [(name, instance) for _, name, instance in command_list]


def escape_html(text: str) -> str:
    """Escape HTML special characters"""
    return html.escape(str(text))


def get_channel_info(cmd_instance: Any, monitor_channels: List[str]) -> Optional[str]:
    """Get channel restriction information for a command"""
    allowed_channels = getattr(cmd_instance, 'allowed_channels', None)
    requires_dm = getattr(cmd_instance, 'requires_dm', False)
    
    # If command has specific allowed channels configured
    if allowed_channels is not None:
        if allowed_channels == []:
            # Empty list means DM only (channels explicitly disabled)
            return "DM only"
        elif len(allowed_channels) > 0:
            # Format channel names (add # if not present)
            formatted_channels = []
            for ch in allowed_channels:
                ch = ch.strip()
                if not ch.startswith('#'):
                    formatted_channels.append(f"#{ch}")
                else:
                    formatted_channels.append(ch)
            if len(formatted_channels) == 1:
                return f"Channel: {', '.join(formatted_channels)}"
            else:
                return f"Channels: {', '.join(formatted_channels)}"
    
    # If command requires DM and has no channel override, it's DM only
    if requires_dm:
        return "DM only"
    
    # No restrictions - works in all monitored channels (don't show anything)
    return None


def format_monitor_channels(monitor_channels: List[str], html: bool = False) -> str:
    """Format monitor channels for display
    
    Args:
        monitor_channels: List of channel names
        html: If True, wrap channel names in span tags for highlighting
    """
    if not monitor_channels:
        return ""
    
    formatted = []
    for ch in monitor_channels:
        ch = ch.strip()
        if not ch.startswith('#'):
            channel_name = f"#{ch}"
        else:
            channel_name = ch
        
        if html:
            # Wrap in span for inline highlighting
            formatted.append(f'<span class="channel-highlight">{escape_html(channel_name)}</span>')
        else:
            formatted.append(channel_name)
    
    if len(formatted) == 1:
        return formatted[0]
    elif len(formatted) == 2:
        return f"{formatted[0]} or {formatted[1]}"
    else:
        return ", ".join(formatted[:-1]) + f", or {formatted[-1]}"


def generate_html(bot_name: str, title: str, introduction: str, commands: List[Tuple[str, Any]], monitor_channels: List[str] = None, channels_data: Dict[str, Dict[str, str]] = None, style: str = 'default') -> str:
    """Generate the HTML content"""
    
    if monitor_channels is None:
        monitor_channels = []
    
    if channels_data is None:
        channels_data = {}
    
    # Format monitor channels message to append to introduction
    channels_suffix = ""
    if monitor_channels:
        formatted_channels = format_monitor_channels(monitor_channels, html=True)
        channels_suffix = f" I'll answer you if you send a message in {formatted_channels}."
    
    # Group commands by category
    categories = defaultdict(list)
    for cmd_name, cmd_instance in commands:
        category = getattr(cmd_instance, 'category', 'general')
        categories[category].append((cmd_name, cmd_instance))
    
    # Category display names
    category_names = {
        'basic': 'Basic Commands',
        'weather': 'Weather Commands',
        'solar': 'Solar & Astronomical',
        'sports': 'Sports',
        'games': 'Games & Entertainment',
        'fun': 'Fun Commands',
        'entertainment': 'Entertainment',
        'meshcore_info': 'Mesh Network Info',
        'analytics': 'Analytics',
        'emergency': 'Emergency',
        'special': 'Special Commands',
        'general': 'General Commands',
    }
    
    # Build navigation sidebar items
    nav_items = []
    
    # Add command categories to nav (basic first, then alphabetically)
    sorted_categories = sorted(categories.keys())
    if 'basic' in sorted_categories:
        sorted_categories.remove('basic')
        sorted_categories.insert(0, 'basic')
    
    for category in sorted_categories:
        category_display = category_names.get(category, category.title().replace('_', ' '))
        category_id = category.lower().replace('_', '-').replace(' ', '-')
        nav_items.append(('commands', category_display, f'commands-{category_id}'))
    
    # Add channels section to nav if available
    if channels_data:
        nav_items.append(('channels', 'Available Channels', 'channels'))
        
        # Add channel subcategories
        sorted_channel_categories = ['general'] + sorted([c for c in channels_data.keys() if c != 'general'])
        for category in sorted_channel_categories:
            if category not in channels_data or not channels_data[category]:
                continue
            category_display = category.title().replace('_', ' ') if category != 'general' else 'General Channels'
            category_id = category.lower().replace('_', '-').replace(' ', '-')
            nav_items.append(('channels-sub', category_display, f'channels-{category_id}'))
    
    # Build navigation sidebar HTML
    nav_html = '<nav class="sidebar-nav">\n'
    nav_html += '  <div class="nav-header">\n'
    nav_html += '    <h3>Navigation</h3>\n'
    nav_html += '  </div>\n'
    nav_html += '  <ul class="nav-list">\n'
    
    # Add Commands section
    if nav_items:
        nav_html += '    <li class="nav-section-header">Commands</li>\n'
        nav_html += '    <ul class="nav-sublist">\n'
        
        for nav_type, display_name, anchor_id in nav_items:
            if nav_type == 'commands':
                nav_html += f'      <li><a href="#{anchor_id}" class="nav-link nav-sublink">{escape_html(display_name)}</a></li>\n'
            elif nav_type == 'channels':
                # Close commands section and start channels
                nav_html += '    </ul>\n'
                nav_html += '    <li class="nav-section-header">Channels</li>\n'
                nav_html += '    <ul class="nav-sublist">\n'
                nav_html += f'      <li><a href="#{anchor_id}" class="nav-link nav-sublink">{escape_html(display_name)}</a></li>\n'
            elif nav_type == 'channels-sub':
                nav_html += f'      <li><a href="#{anchor_id}" class="nav-link nav-sublink">{escape_html(display_name)}</a></li>\n'
        
        nav_html += '    </ul>\n'
    
    nav_html += '  </ul>\n'
    nav_html += '</nav>\n'
    
    # Build command HTML (basic first, then alphabetically)
    commands_html = ""
    sorted_command_categories = sorted(categories.keys())
    if 'basic' in sorted_command_categories:
        sorted_command_categories.remove('basic')
        sorted_command_categories.insert(0, 'basic')
    
    for category in sorted_command_categories:
        category_commands = categories[category]
        category_display = category_names.get(category, category.title().replace('_', ' '))
        # Create anchor ID from category (lowercase, replace spaces with hyphens)
        category_id = category.lower().replace('_', '-').replace(' ', '-')
        
        commands_html += f'<div class="category-section" id="commands-{category_id}">\n'
        commands_html += f'  <h2 class="category-title"><a href="#commands-{category_id}" class="anchor-link">{escape_html(category_display)}</a></h2>\n'
        commands_html += f'  <div class="commands-grid">\n'
        
        for cmd_name, cmd_instance in category_commands:
            primary_name = cmd_instance.name if hasattr(cmd_instance, 'name') else cmd_name
            keywords = getattr(cmd_instance, 'keywords', [])
            
            # Filter out the primary name from keywords to avoid duplication
            aliases = [k for k in keywords if k.lower() != primary_name.lower()]
            
            # Get channel restriction info
            channel_info = get_channel_info(cmd_instance, monitor_channels)
            
            # Get usage information including usage syntax, examples, parameters, and sub-commands
            try:
                usage_info = cmd_instance.get_usage_info()
                usage_syntax = usage_info.get('usage', '')
                examples = usage_info.get('examples', [])
                parameters = usage_info.get('parameters', [])
                subcommands = usage_info.get('subcommands', [])
                # Use short_description for website if available, otherwise fallback to description
                short_desc = usage_info.get('short_description', '')
                description = short_desc if short_desc else usage_info.get('description', 'No description available')
            except Exception:
                usage_syntax = ''
                examples = []
                parameters = []
                subcommands = []
                description = getattr(cmd_instance, 'description', 'No description available')
            
            commands_html += f'    <div class="command-card">\n'
            commands_html += f'      <div class="command-header">\n'
            commands_html += f'        <h3 class="command-name">{escape_html(primary_name)}</h3>\n'
            if aliases:
                commands_html += f'        <div class="command-keywords">\n'
                # Show first 5 aliases
                visible_aliases = aliases[:5]
                hidden_aliases = aliases[5:]
                
                for alias in visible_aliases:
                    commands_html += f'          <span class="keyword-badge">{escape_html(alias)}</span>\n'
                
                if hidden_aliases:
                    # Store hidden aliases in data attribute and create expandable badge
                    hidden_aliases_json = escape_html(','.join(hidden_aliases))
                    commands_html += f'          <span class="keyword-badge keyword-expand" data-hidden="{hidden_aliases_json}" data-command="{escape_html(primary_name)}">+{len(hidden_aliases)} more</span>\n'
                
                commands_html += f'        </div>\n'
            commands_html += f'      </div>\n'
            commands_html += f'      <p class="command-description">{escape_html(description)}</p>\n'
            
            # Render usage, examples, parameters, subcommands
            try:
                
                # Render usage syntax
                if usage_syntax:
                    commands_html += f'      <div class="command-usage"><code>{escape_html(usage_syntax)}</code></div>\n'
                
                # Render parameters
                if parameters:
                    commands_html += f'      <div class="command-params">\n'
                    commands_html += f'        <div class="params-header">Parameters:</div>\n'
                    for param in parameters:
                        param_name = param.get('name', '')
                        param_desc = param.get('description', '')
                        if param_name and param_desc:
                            commands_html += f'        <div class="param-item">\n'
                            commands_html += f'          <span class="param-name">{escape_html(param_name)}</span>\n'
                            commands_html += f'          <span class="param-desc">{escape_html(param_desc)}</span>\n'
                            commands_html += f'        </div>\n'
                    commands_html += f'      </div>\n'
                
                # Render subcommands
                if subcommands:
                    commands_html += f'      <div class="command-subcommands">\n'
                    commands_html += f'        <div class="subcommands-header">Sub-commands:</div>\n'
                    for subcmd in subcommands:
                        subcmd_name = subcmd.get('name', '')
                        subcmd_desc = subcmd.get('description', '')
                        if subcmd_name and subcmd_desc:
                            commands_html += f'        <div class="subcommand-item">\n'
                            commands_html += f'          <span class="subcommand-name">{escape_html(subcmd_name)}</span>\n'
                            commands_html += f'          <span class="subcommand-desc">{escape_html(subcmd_desc)}</span>\n'
                            commands_html += f'        </div>\n'
                    commands_html += f'      </div>\n'
            except Exception as e:
                # Silently fail if get_usage_info() is not available or fails
                pass
            
            if channel_info:
                commands_html += f'      <div class="command-channels">{escape_html(channel_info)}</div>\n'
            commands_html += f'    </div>\n'
        
        commands_html += f'  </div>\n'
        commands_html += f'</div>\n'
    
    # Build channels HTML if channels are available
    channels_html = ""
    if channels_data:
        channels_html += '<div class="category-section" id="channels">\n'
        channels_html += '  <h2 class="category-title"><a href="#channels" class="anchor-link">Available Channels</a></h2>\n'
        channels_html += '  <p class="channels-intro">These are semi-public channels that are in use on our local mesh! Join them to connect with others with common interests! <br/> To add these channels to your client, click on the three dot menu and select "Add Channel".</p>\n'

        # Sort categories: general first, then alphabetically
        sorted_categories = ['general'] + sorted([c for c in channels_data.keys() if c != 'general'])
        
        for category in sorted_categories:
            if category not in channels_data or not channels_data[category]:
                continue
            
            category_display = category.title().replace('_', ' ') if category != 'general' else 'General Channels'
            category_id = category.lower().replace('_', '-').replace(' ', '-')
            channels_html += f'  <div class="channel-category" id="channels-{category_id}">\n'
            channels_html += f'    <h3 class="channel-category-title"><a href="#channels-{category_id}" class="anchor-link">{escape_html(category_display)}</a></h3>\n'
            channels_html += f'    <div class="channels-grid">\n'
            
            # Sort channels alphabetically
            sorted_channels = sorted(channels_data[category].items())
            for channel_name, description in sorted_channels:
                channels_html += f'      <div class="channel-card">\n'
                channels_html += f'        <div class="channel-name">{escape_html(channel_name)}</div>\n'
                channels_html += f'        <div class="channel-description">{escape_html(description)}</div>\n'
                channels_html += f'      </div>\n'
            
            channels_html += f'    </div>\n'
            channels_html += f'  </div>\n'
        
        channels_html += '</div>\n'
    
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escape_html(title)}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="{STYLES[style]['fonts_url']}" rel="stylesheet">
    <style>
        :root {{
            {STYLES[style]['css_vars']}
        }}
        
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        html {{
            scroll-behavior: smooth;
        }}
        
        body {{
            font-family: 'Outfit', sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            overflow-x: hidden;
            line-height: 1.6;
        }}
        
        /* Atmospheric background */
        .atmosphere {{
            position: fixed;
            inset: 0;
            background: 
                radial-gradient(ellipse 80% 50% at 20% 0%, rgba(0, 212, 255, 0.08) 0%, transparent 50%),
                radial-gradient(ellipse 60% 40% at 80% 100%, rgba(0, 255, 200, 0.05) 0%, transparent 50%),
                radial-gradient(ellipse 100% 100% at 50% 50%, var(--bg-primary) 0%, #060a0f 100%);
            pointer-events: none;
            z-index: -1;
        }}
        
        /* Subtle grid overlay */
        .grid-overlay {{
            position: fixed;
            inset: 0;
            background-image: 
                linear-gradient(rgba(255,255,255,0.01) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,255,255,0.01) 1px, transparent 1px);
            background-size: 60px 60px;
            pointer-events: none;
            z-index: -1;
        }}
        
        .container {{
            max-width: 1600px;
            margin: 0 auto;
            padding: 3rem 2rem;
            min-height: 100vh;
            position: relative;
            z-index: 1;
            display: grid;
            grid-template-columns: 280px 1fr;
            gap: 3rem;
        }}
        
        .sidebar-nav {{
            position: sticky;
            top: 2rem;
            height: fit-content;
            max-height: calc(100vh - 4rem);
            overflow-y: auto;
            background: var(--bg-card);
            border-radius: 16px;
            border: 1px solid var(--border-subtle);
            padding: 1.5rem;
            z-index: 100;
            transition: left 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            opacity: 1;
            filter: none;
        }}
        
        .mobile-menu-toggle {{
            display: none;
            position: fixed;
            top: 1.5rem;
            right: 1.5rem;
            z-index: 1001;
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 12px;
            padding: 0.75rem;
            cursor: pointer;
            flex-direction: column;
            gap: 5px;
            align-items: center;
            justify-content: center;
            width: 48px;
            height: 48px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
            transition: all 0.3s ease;
        }}
        
        .mobile-menu-toggle:hover {{
            background: var(--bg-card-hover);
            border-color: var(--accent-cyan);
        }}
        
        .mobile-menu-toggle.active {{
            background: var(--bg-card-hover);
            border-color: var(--accent-cyan);
        }}
        
        .hamburger {{
            width: 24px;
            height: 2px;
            background: var(--accent-cyan);
            border-radius: 2px;
            transition: all 0.3s ease;
        }}
        
        .mobile-menu-toggle.active .hamburger:nth-child(1) {{
            transform: rotate(45deg) translate(7px, 7px);
        }}
        
        .mobile-menu-toggle.active .hamburger:nth-child(2) {{
            opacity: 0;
        }}
        
        .mobile-menu-toggle.active .hamburger:nth-child(3) {{
            transform: rotate(-45deg) translate(7px, -7px);
        }}
        
        .sidebar-overlay {{
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.7);
            z-index: 1000;
            backdrop-filter: none;
            -webkit-backdrop-filter: none;
            pointer-events: none;
        }}
        
        .sidebar-overlay.visible {{
            pointer-events: auto;
        }}
        
        @media (max-width: 1200px) {{
            .sidebar-overlay.visible {{
                /* Don't cover the sidebar area - start overlay after sidebar width */
                left: 280px !important;
            }}
        }}
        
        @media (min-width: 1201px) {{
            .sidebar-overlay {{
                backdrop-filter: blur(4px);
                -webkit-backdrop-filter: blur(4px);
            }}
        }}
        
        @media (max-width: 1200px) {{
            .sidebar-overlay {{
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
                z-index: 1000 !important;
            }}
            
            .sidebar-overlay.visible {{
                pointer-events: auto !important;
            }}
            
            /* Ensure sidebar is always clickable above overlay */
            .sidebar-nav {{
                pointer-events: auto !important;
            }}
            
            .sidebar-nav * {{
                pointer-events: auto !important;
            }}
        }}
        
        .sidebar-overlay.visible {{
            display: block;
        }}
        
        .sidebar-nav::-webkit-scrollbar {{
            width: 6px;
        }}
        
        .sidebar-nav::-webkit-scrollbar-track {{
            background: var(--bg-secondary);
            border-radius: 3px;
        }}
        
        .sidebar-nav::-webkit-scrollbar-thumb {{
            background: var(--border-subtle);
            border-radius: 3px;
        }}
        
        .sidebar-nav::-webkit-scrollbar-thumb:hover {{
            background: rgba(255,255,255,0.15);
        }}
        
        .nav-header {{
            margin-bottom: 1.5rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--border-subtle);
        }}
        
        .nav-header h3 {{
            font-size: 1rem;
            font-weight: 600;
            color: var(--text-primary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            font-size: 0.85rem;
        }}
        
        .nav-list {{
            list-style: none;
            padding: 0;
            margin: 0;
        }}
        
        .nav-section-header {{
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: var(--text-muted);
            font-weight: 600;
            margin-top: 1.5rem;
            margin-bottom: 0.75rem;
            padding-left: 0.5rem;
        }}
        
        .nav-section-header:first-child {{
            margin-top: 0;
        }}
        
        .nav-sublist {{
            list-style: none;
            padding: 0;
            margin: 0 0 1rem 0;
            padding-left: 0.5rem;
        }}
        
        .nav-link {{
            display: block;
            padding: 0.6rem 0.75rem;
            color: var(--text-secondary);
            text-decoration: none;
            border-radius: 8px;
            font-size: 0.9rem;
            transition: all 0.2s ease;
            margin-bottom: 0.25rem;
            cursor: pointer;
            -webkit-tap-highlight-color: rgba(0, 255, 200, 0.2);
        }}
        
        .nav-link:hover {{
            background: var(--bg-card-hover);
            color: var(--accent-cyan);
            transform: translateX(4px);
        }}
        
        .nav-link:active,
        .nav-link.active {{
            background: rgba(0, 255, 200, 0.1);
            color: var(--accent-cyan);
            border-left: 3px solid var(--accent-cyan);
            padding-left: calc(0.75rem - 3px);
        }}
        
        .nav-sublink {{
            padding-left: 1.25rem;
            font-size: 0.85rem;
            color: var(--text-muted);
        }}
        
        .nav-sublink:hover {{
            color: var(--accent-blue);
        }}
        
        .main-content {{
            min-width: 0;
        }}
        
        header {{
            margin-bottom: 4rem;
            padding: 0;
        }}
        
        .header-content {{
            background: var(--bg-card);
            border-radius: 20px;
            border: 1px solid var(--border-subtle);
            padding: 3rem 2.5rem;
            position: relative;
            overflow: hidden;
        }}
        
        .header-content::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: linear-gradient(90deg, var(--accent-blue), var(--accent-cyan));
        }}
        
        .header-title {{
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 1.5rem;
        }}
        
        .logo-icon {{
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 3.5rem;
            flex-shrink: 0;
            line-height: 1;
        }}
        
        h1 {{
            font-size: 3rem;
            font-weight: 600;
            margin: 0;
            color: var(--accent-cyan);
            letter-spacing: -0.02em;
            font-family: 'Outfit', sans-serif;
        }}
        
        .intro {{
            font-size: 1.1rem;
            color: var(--text-secondary);
            max-width: 900px;
            line-height: 1.8;
            margin-top: 0.5rem;
        }}
        
        .channel-highlight {{
            color: var(--accent-cyan);
            font-weight: 500;
        }}
        
        .category-section {{
            margin-bottom: 3rem;
        }}
        
        .category-title {{
            font-size: 1.75rem;
            font-weight: 600;
            margin-bottom: 1.5rem;
            color: var(--text-primary);
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--border-subtle);
            letter-spacing: -0.01em;
        }}
        
        .category-title .anchor-link,
        .channel-category-title .anchor-link {{
            color: inherit;
            text-decoration: none;
            position: relative;
            display: inline-block;
        }}
        
        .category-title .anchor-link:hover,
        .channel-category-title .anchor-link:hover {{
            color: var(--accent-cyan);
        }}
        
        .category-title .anchor-link::before {{
            content: '#';
            position: absolute;
            left: -1.5rem;
            opacity: 0;
            color: var(--accent-blue);
            font-weight: 400;
            transition: opacity 0.2s ease;
        }}
        
        .category-title:hover .anchor-link::before,
        .channel-category-title:hover .anchor-link::before {{
            opacity: 0.5;
        }}
        
        .category-section,
        .channel-category {{
            scroll-margin-top: 2rem;
        }}
        
        .commands-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 1.25rem;
            margin-bottom: 2rem;
        }}
        
        .command-card {{
            background: var(--bg-card);
            border-radius: 16px;
            padding: 1.5rem;
            border: 1px solid var(--border-subtle);
            transition: all 0.3s ease;
            position: relative;
            overflow: hidden;
        }}
        
        .command-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: linear-gradient(90deg, var(--accent-blue), transparent);
            opacity: 0;
            transition: opacity 0.3s ease;
        }}
        
        .command-card:hover {{
            background: var(--bg-card-hover);
            border-color: rgba(255,255,255,0.1);
            transform: translateY(-2px);
        }}
        
        .command-card:hover::before {{
            opacity: 1;
        }}
        
        .command-header {{
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            margin-bottom: 1rem;
        }}
        
        .command-name {{
            font-size: 1.5rem;
            font-weight: 600;
            color: var(--accent-blue);
            margin: 0;
            letter-spacing: -0.01em;
        }}
        
        .command-keywords {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
        }}
        
        .keyword-badge {{
            background: rgba(0, 212, 255, 0.1);
            color: var(--accent-cyan);
            padding: 0.35rem 0.75rem;
            border-radius: 8px;
            font-size: 0.8rem;
            font-weight: 500;
            border: 1px solid rgba(0, 212, 255, 0.2);
            font-family: 'JetBrains Mono', monospace;
        }}
        
        .keyword-expand {{
            cursor: pointer;
            transition: all 0.2s ease;
        }}
        
        .keyword-expand:hover {{
            background: rgba(0, 212, 255, 0.2);
            border-color: var(--accent-cyan);
            transform: scale(1.05);
        }}
        
        .keyword-expand.expanded {{
            display: none;
        }}
        
        .keyword-hidden {{
            display: none;
        }}
        
        .keyword-hidden.visible {{
            display: inline-block;
        }}
        
        .command-description {{
            color: var(--text-secondary);
            line-height: 1.7;
            margin-bottom: 0.75rem;
            font-size: 0.95rem;
        }}
        
        .command-usage {{
            margin: 0.75rem 0;
            padding: 0.6rem 0.9rem;
            background: var(--bg-secondary);
            border-radius: 8px;
            border-left: 3px solid var(--accent-blue);
        }}
        
        .command-usage code {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.85rem;
            color: var(--accent-cyan);
        }}
        
        .command-params {{
            margin-top: 0.75rem;
            padding-top: 0.75rem;
            border-top: 1px solid var(--border-subtle);
        }}
        
        .params-header {{
            font-size: 0.8rem;
            font-weight: 600;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        
        .param-item {{
            display: flex;
            gap: 0.5rem;
            margin-bottom: 0.4rem;
            font-size: 0.85rem;
            align-items: flex-start;
        }}
        
        .param-name {{
            font-family: 'JetBrains Mono', monospace;
            color: var(--accent-orange);
            font-weight: 500;
            min-width: 80px;
            flex-shrink: 0;
        }}
        
        .param-desc {{
            color: var(--text-secondary);
            flex: 1;
            line-height: 1.4;
        }}
        
        .command-subcommands {{
            margin-top: 1rem;
            padding-top: 1rem;
            border-top: 1px solid var(--border-subtle);
        }}
        
        .subcommands-header {{
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-muted);
            margin-bottom: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        
        .subcommand-item {{
            display: flex;
            gap: 0.75rem;
            margin-bottom: 0.5rem;
            font-size: 0.9rem;
            align-items: flex-start;
        }}
        
        .subcommand-name {{
            font-family: 'JetBrains Mono', monospace;
            color: var(--accent-cyan);
            font-weight: 500;
            min-width: 100px;
            flex-shrink: 0;
        }}
        
        .subcommand-desc {{
            color: var(--text-secondary);
            flex: 1;
            line-height: 1.5;
        }}
        
        .command-channels {{
            color: var(--accent-cyan);
            font-size: 0.85rem;
            margin-top: 0.75rem;
            padding: 0.6rem 0.9rem;
            background: rgba(0, 255, 200, 0.08);
            border-radius: 8px;
            border-left: 3px solid var(--accent-cyan);
            font-family: 'JetBrains Mono', monospace;
            font-weight: 500;
        }}
        
        .channels-intro {{
            color: var(--text-secondary);
            font-size: 1rem;
            margin-bottom: 2rem;
            line-height: 1.7;
        }}
        
        .channel-category {{
            margin-bottom: 2.5rem;
        }}
        
        .channel-category-title {{
            font-size: 1.25rem;
            font-weight: 600;
            color: var(--accent-blue);
            margin-bottom: 1rem;
            letter-spacing: -0.01em;
        }}
        
        .channel-category-title .anchor-link::before {{
            content: '#';
            position: absolute;
            left: -1.2rem;
            opacity: 0;
            color: var(--accent-blue);
            font-weight: 400;
            transition: opacity 0.2s ease;
        }}
        
        .channels-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 1rem;
        }}
        
        .channel-card {{
            background: var(--bg-card);
            border-radius: 12px;
            padding: 1.25rem;
            border: 1px solid var(--border-subtle);
            transition: all 0.3s ease;
        }}
        
        .channel-card:hover {{
            background: var(--bg-card-hover);
            border-color: rgba(0, 212, 255, 0.3);
            transform: translateY(-2px);
        }}
        
        .channel-name {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 1.1rem;
            font-weight: 600;
            color: var(--accent-cyan);
            margin-bottom: 0.5rem;
        }}
        
        .channel-description {{
            color: var(--text-secondary);
            font-size: 0.9rem;
            line-height: 1.6;
        }}
        
        footer {{
            text-align: center;
            margin-top: 4rem;
            padding: 2rem;
            color: var(--text-muted);
            font-size: 0.9rem;
            border-top: 1px solid var(--border-subtle);
        }}
        
        @media (max-width: 1200px) {{
            .commands-grid {{
                grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            }}
        }}
        
        @media (max-width: 1200px) {{
            /* Disable all backdrop filters on mobile */
            * {{
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
            }}
            
            .container {{
                grid-template-columns: 1fr !important;
            }}
            
            .mobile-menu-toggle {{
                display: flex;
            }}
            
            .sidebar-nav {{
                position: fixed !important;
                top: 0 !important;
                left: -320px !important;
                width: 280px !important;
                height: 100vh !important;
                max-height: 100vh !important;
                margin: 0 !important;
                padding-top: 4rem !important;
                border-radius: 0 !important;
                border-left: none !important;
                border-top: none !important;
                border-bottom: none !important;
                border-right: 1px solid var(--border-subtle) !important;
                transition: left 0.3s cubic-bezier(0.4, 0, 0.2, 1) !important;
                z-index: 1002 !important;
                opacity: 1 !important;
                filter: none !important;
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
                transform: translateZ(0) !important;
                will-change: left !important;
                isolation: isolate !important;
                background: var(--bg-card) !important;
                visibility: visible !important;
                -webkit-font-smoothing: antialiased !important;
                -moz-osx-font-smoothing: grayscale !important;
                overflow-y: auto !important;
                overflow-x: hidden !important;
                -webkit-overflow-scrolling: touch !important;
                pointer-events: auto !important;
                touch-action: pan-y !important;
                -webkit-transform: translateZ(0) !important;
                transform: translateZ(0) !important;
            }}
            
            .sidebar-nav.open {{
                left: 0 !important;
            }}
            
            .sidebar-nav * {{
                opacity: 1 !important;
                filter: none !important;
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
                visibility: visible !important;
                -webkit-font-smoothing: antialiased !important;
                -moz-osx-font-smoothing: grayscale !important;
                pointer-events: auto !important;
            }}
            
            .sidebar-nav .nav-header h3 {{
                color: #e8edf4 !important;
                opacity: 1 !important;
                text-shadow: none !important;
            }}
            
            .sidebar-nav .nav-link {{
                color: #8892a4 !important;
                opacity: 1 !important;
                text-shadow: none !important;
                pointer-events: auto !important;
                cursor: pointer !important;
                -webkit-tap-highlight-color: rgba(0, 255, 200, 0.2) !important;
            }}
            
            .sidebar-nav .nav-link:hover,
            .sidebar-nav .nav-link:active {{
                color: #00ffc8 !important;
                opacity: 1 !important;
            }}
            
            .sidebar-nav .nav-section-header {{
                color: #8892a4 !important;
                opacity: 1 !important;
                text-shadow: none !important;
            }}
            
            .main-content {{
                margin-top: 0;
            }}
        }}
        
        @media (min-width: 1201px) {{
            .sidebar-nav {{
                position: sticky !important;
                top: 2rem !important;
                left: auto !important;
                width: auto !important;
                height: fit-content !important;
                max-height: calc(100vh - 4rem) !important;
                margin: 0 !important;
                padding: 1.5rem !important;
                border-radius: 16px !important;
                border: 1px solid var(--border-subtle) !important;
                z-index: 100 !important;
                opacity: 1 !important;
                filter: none !important;
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
                transform: none !important;
                will-change: auto !important;
                isolation: auto !important;
                background: var(--bg-card) !important;
                visibility: visible !important;
            }}
        }}
        
        @media (max-width: 768px) {{
            .container {{
                padding: 2rem 1rem;
            }}
            
            .header-content {{
                padding: 2rem 1.5rem;
            }}
            
            .header-title {{
                flex-direction: column;
                align-items: flex-start;
                gap: 0.75rem;
            }}
            
            .logo-icon {{
                width: 48px;
                height: 48px;
                font-size: 1.5rem;
            }}
            
            h1 {{
                font-size: 2.25rem;
            }}
            
            .commands-grid {{
                grid-template-columns: 1fr;
            }}
            
            .sidebar-nav {{
                padding: 1rem;
                width: calc(100vw - 3rem);
                left: calc(-100vw + 3rem);
            }}
            
            .sidebar-nav.open {{
                left: 0;
            }}
            
            .mobile-menu-toggle {{
                top: 1rem;
                right: 1rem;
            }}
        }}

        /* Style-specific CSS overrides */
        {STYLES[style].get('css_overrides', '')}
    </style>
</head>
<body>
    <div class="atmosphere"></div>
    <div class="grid-overlay"></div>
    
    <div class="sidebar-overlay"></div>
    
    <button class="mobile-menu-toggle" aria-label="Toggle navigation menu">
        <span class="hamburger"></span>
        <span class="hamburger"></span>
        <span class="hamburger"></span>
    </button>
    
    <div class="container">
        {nav_html}
        
        <div class="main-content">
            <header>
                <div class="header-content">
                    <div class="header-title">
                        <h1>{escape_html(bot_name)}</h1>
                    </div>
                    <div class="intro">
                        {escape_html(introduction).replace(chr(10), '<br>')}{channels_suffix}
                    </div>
                </div>
            </header>
            
            <main>
                {commands_html}
                {channels_html}
            </main>
            
            <footer>
                <p>Generated command reference for {escape_html(bot_name)}</p>
            </footer>
        </div>
    </div>
    
    <script>
        // Handle mobile menu toggle
        (function() {{
            'use strict';
            
            const menuToggle = document.querySelector('.mobile-menu-toggle');
            const sidebar = document.querySelector('.sidebar-nav');
            const overlay = document.querySelector('.sidebar-overlay');
            
            if (menuToggle && sidebar) {{
                function toggleMenu() {{
                    const isOpen = sidebar.classList.contains('open');
                    if (isOpen) {{
                        menuToggle.classList.remove('active');
                        sidebar.classList.remove('open');
                        if (overlay) {{
                            overlay.classList.remove('visible');
                        }}
                    }} else {{
                        menuToggle.classList.add('active');
                        sidebar.classList.add('open');
                        if (overlay) {{
                            overlay.classList.add('visible');
                        }}
                    }}
                }}
                
                function closeMenu() {{
                    menuToggle.classList.remove('active');
                    sidebar.classList.remove('open');
                    if (overlay) {{
                        overlay.classList.remove('visible');
                    }}
                }}
                
                menuToggle.addEventListener('click', function(e) {{
                    e.stopPropagation();
                    e.preventDefault();
                    toggleMenu();
                    return false;
                }});
                
                // Handle overlay clicks - overlay now only covers area after sidebar
                if (overlay) {{
                    overlay.addEventListener('click', function(e) {{
                        closeMenu();
                    }});
                }}
                
                // Handle nav link clicks - don't prevent default, let browser handle navigation
                const navLinks = sidebar.querySelectorAll('.nav-link');
                navLinks.forEach(link => {{
                    link.addEventListener('click', function(e) {{
                        if (window.innerWidth <= 1200) {{
                            // Close menu after a delay to allow navigation
                            setTimeout(function() {{
                                closeMenu();
                            }}, 200);
                        }}
                    }});
                }});
            }}
            
            // Handle keyword expansion
            const expandButtons = document.querySelectorAll('.keyword-expand');
            
            expandButtons.forEach(button => {{
                button.addEventListener('click', function() {{
                    const hiddenAliases = this.getAttribute('data-hidden').split(',');
                    const commandKeywords = this.closest('.command-keywords');
                    
                    // Hide the expand button
                    this.classList.add('expanded');
                    
                    // Add hidden aliases as visible badges
                    hiddenAliases.forEach(alias => {{
                        const badge = document.createElement('span');
                        badge.className = 'keyword-badge keyword-hidden visible';
                        badge.textContent = alias.trim();
                        commandKeywords.appendChild(badge);
                    }});
                }});
            }});
        }})();
    </script>
</body>
</html>"""
    
    return html_content


def list_styles():
    """Print available styles with descriptions."""
    print("Available styles:\n")

    # Calculate max name length for alignment
    max_len = max(len(name) for name in STYLES.keys())

    for style_name, style_info in STYLES.items():
        padding = ' ' * (max_len - len(style_name))
        print(f"  {style_name}{padding}  {style_info['name']}")
        print(f"  {' ' * max_len}  {style_info['description']}\n")


def generate_samples(config_file):
    """Generate sample HTML files for all styles with an index page."""
    logger = logging.getLogger(__name__)

    logger.info(f"Reading config from {config_file}")
    config = read_config(config_file)

    # Get bot root directory
    bot_root = os.path.dirname(os.path.abspath(config_file))
    if not bot_root:
        bot_root = os.getcwd()

    # Get bot information
    bot_name = get_bot_name(config)
    admin_commands = get_admin_commands(config)
    title = get_website_title(config)
    introduction = get_website_intro(config)

    # Get monitor channels (quoted or unquoted)
    monitor_channels_str = strip_optional_quotes(config.get('Channels', 'monitor_channels', fallback=''))
    monitor_channels = [ch.strip() for ch in monitor_channels_str.split(',') if ch.strip()]

    # Load channels from config
    channels_data = load_channels_from_config(config)

    # Setup minimal bot for plugin loading
    minimal_bot = MinimalBot(config, logger)

    # Initialize database manager if database exists
    db_path = get_database_path(config, bot_root)
    if db_path and os.path.exists(db_path):
        try:
            minimal_bot.db_manager = DBManager(minimal_bot, db_path)
        except Exception as e:
            logger.warning(f"Could not load database: {e}")
            minimal_bot.db_manager = None
    else:
        minimal_bot.db_manager = None

    # Load plugins
    plugin_loader = PluginLoader(minimal_bot)
    commands = plugin_loader.load_all_plugins()

    # Filter out admin and hidden commands
    public_commands = {
        name: cmd for name, cmd in commands.items()
        if name not in admin_commands and not getattr(cmd, 'hidden', False)
    }

    # Sort commands
    sorted_commands = sorted(public_commands.items(), key=lambda x: x[0])

    # Create website directory
    output_dir = os.path.join(bot_root, 'website')
    os.makedirs(output_dir, exist_ok=True)

    # Generate a page for each style
    generated_files = []
    for style_name in STYLES.keys():
        logger.info(f"Generating {style_name}.html...")

        html_content = generate_html(
            bot_name=bot_name,
            title=title,
            introduction=introduction,
            commands=sorted_commands,
            monitor_channels=monitor_channels,
            channels_data=channels_data,
            style=style_name
        )

        output_path = os.path.join(output_dir, f'{style_name}.html')
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        generated_files.append(style_name)
        logger.info(f"  ✓ {output_path}")

    # Generate index.html with links to all styles
    logger.info("Generating index.html...")

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escape_html(bot_name)} - Style Samples</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            line-height: 1.6;
            padding: 3rem 2rem;
            max-width: 800px;
            margin: 0 auto;
            background: #f5f5f5;
        }}

        h1 {{
            font-size: 2.5rem;
            margin-bottom: 0.5rem;
            color: #1a1a1a;
        }}

        p {{
            color: #666;
            margin-bottom: 2rem;
        }}

        .styles-grid {{
            display: grid;
            gap: 1rem;
        }}

        .style-link {{
            display: block;
            padding: 1.5rem;
            background: white;
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            text-decoration: none;
            transition: all 0.2s ease;
        }}

        .style-link:hover {{
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            border-color: #0052cc;
        }}

        .style-name {{
            font-size: 1.25rem;
            font-weight: 600;
            color: #0052cc;
            margin-bottom: 0.25rem;
        }}

        .style-description {{
            color: #666;
            font-size: 0.875rem;
        }}
    </style>
</head>
<body>
    <h1>{escape_html(bot_name)} Style Samples</h1>
    <p>Browse different visual styles for the command documentation.</p>

    <div class="styles-grid">
"""

    for style_name in STYLES.keys():
        style_info = STYLES[style_name]
        index_html += f"""        <a href="{style_name}.html" class="style-link">
            <div class="style-name">{escape_html(style_info['name'])}</div>
            <div class="style-description">{escape_html(style_info['description'])}</div>
        </a>
"""

    index_html += """    </div>
</body>
</html>
"""

    index_path = os.path.join(output_dir, 'index.html')
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(index_html)

    logger.info(f"  ✓ {index_path}")
    logger.info(f"\nGenerated {len(generated_files)} style samples + index.html in {output_dir}")
    print(f"\n✓ Sample files generated in {output_dir}/")
    print(f"  - index.html (style browser)")
    for style_name in generated_files:
        print(f"  - {style_name}.html")


def main():
    """Main function"""
    logger = setup_logging()

    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description='Generate a static website for the bot commands and channels.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        'config',
        nargs='?',
        default='config.ini',
        help='Path to config.ini file (default: config.ini)'
    )

    parser.add_argument(
        '--style',
        choices=list(STYLES.keys()),
        default='default',
        help='CSS style theme for the website (default: default)'
    )

    parser.add_argument(
        '--list-styles',
        action='store_true',
        help='List all available styles and exit'
    )

    parser.add_argument(
        '--sample',
        action='store_true',
        help='Generate sample pages for all styles with an index.html'
    )

    args = parser.parse_args()

    # Handle --list-styles flag
    if args.list_styles:
        list_styles()
        sys.exit(0)

    # Handle --sample flag
    if args.sample:
        generate_samples(args.config)
        sys.exit(0)

    config_file = args.config
    style = args.style
    
    try:
        # Read config
        logger.info(f"Reading config from {config_file}")
        config = read_config(config_file)
        
        # Get bot root directory
        bot_root = os.path.dirname(os.path.abspath(config_file))
        if not bot_root:
            bot_root = os.getcwd()
        
        # Get bot information
        bot_name = get_bot_name(config)
        admin_commands = get_admin_commands(config)
        introduction = get_website_intro(config)
        title = get_website_title(config)
        
        # Get monitor channels for channel restriction display (quoted or unquoted)
        monitor_channels_str = strip_optional_quotes(config.get('Channels', 'monitor_channels', fallback=''))
        monitor_channels = [ch.strip() for ch in monitor_channels_str.split(',') if ch.strip()]
        
        # Load channels from Channels_List section
        channels_data = load_channels_from_config(config)
        logger.info(f"Loaded {sum(len(chans) for chans in channels_data.values())} channels from {len(channels_data)} categories")
        
        logger.info(f"Bot name: {bot_name}")
        logger.info(f"Admin commands: {admin_commands}")
        logger.info(f"Monitor channels: {monitor_channels}")
        
        # Setup minimal bot for plugin loading
        minimal_bot = MinimalBot(config, logger)
        
        # Initialize database manager if database exists
        db_path = get_database_path(config, bot_root)
        if db_path and os.path.exists(db_path):
            try:
                minimal_bot.db_manager = DBManager(minimal_bot, db_path)
                logger.info(f"Database loaded: {db_path}")
            except Exception as e:
                logger.warning(f"Could not load database: {e}")
                minimal_bot.db_manager = None
        else:
            logger.info("No database found, using default command ordering")
            minimal_bot.db_manager = None
        
        # Load plugins
        logger.info("Loading command plugins...")
        plugin_loader = PluginLoader(minimal_bot)
        commands = plugin_loader.load_all_plugins()
        logger.info(f"Loaded {len(commands)} commands")
        
        # Filter out admin and hidden commands
        filtered_commands = filter_commands(commands, admin_commands)
        logger.info(f"Filtered to {len(filtered_commands)} public commands")
        
        # Log which commands are included
        included_names = sorted([cmd.name if hasattr(cmd, 'name') else name for name, cmd in filtered_commands.items()])
        logger.info(f"Included commands: {', '.join(included_names)}")
        
        # Log which commands were excluded (for debugging)
        excluded = []
        for cmd_name, cmd_instance in commands.items():
            if cmd_name not in filtered_commands:
                primary_name = cmd_instance.name if hasattr(cmd_instance, 'name') else cmd_name
                category = getattr(cmd_instance, 'category', 'unknown')
                excluded.append(f"{primary_name} ({category})")
        if excluded:
            logger.debug(f"Excluded commands: {', '.join(excluded)}")
        
        # Get command popularity
        popularity = get_command_popularity(db_path, filtered_commands)
        
        # Sort commands by popularity
        sorted_commands = sort_commands_by_popularity(filtered_commands, popularity)
        
        # Generate HTML
        logger.info("Generating HTML...")
        html_content = generate_html(bot_name, title, introduction, sorted_commands, monitor_channels, channels_data, style)
        
        # Create website directory
        website_dir = os.path.join(bot_root, "website")
        os.makedirs(website_dir, exist_ok=True)
        
        # Write HTML file
        output_file = os.path.join(website_dir, "index.html")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        logger.info(f"Website generated successfully: {output_file}")
        print(f"\n✓ Website generated: {output_file}")
        print(f"  Bot: {bot_name}")
        print(f"  Commands: {len(sorted_commands)}")
        print(f"  Output: {output_file}")
        
    except Exception as e:
        logger.error(f"Error generating website: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
