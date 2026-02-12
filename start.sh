#!/bin/bash
# Intentar forzar el uso de python3.11 si está disponible en el sistema, 
# o usar el alias de python predeterminado si Render no lo ha sobreescrito correctamente.
if command -v python3.11 &> /dev/null; then
    echo "Forzando uso de Python 3.11..."
    python3.11 -m pip install -r requirements.txt
    python3.11 main.py
else
    echo "Python 3.11 no encontrado en path, intentando fallback..."
    python main.py
fi
