#!/bin/bash

echo "Iniciando configuración forzada para Python 3.11..."

# 1. Forzar la instalación de librerías usando Python 3.11
# Usamos --break-system-packages porque en Render el entorno 3.11 es gestionado por el sistema
python3.11 -m pip install -r requirements.txt --break-system-packages

# 2. Ejecutar el bot principal usando Python 3.11
echo "Ejecutando bot con Python 3.11..."
python3.11 main.py
