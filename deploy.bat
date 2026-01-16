@echo off
echo ========================================
echo    DEPLOIEMENT AUTOMATIQUE
echo ========================================
echo.

echo [1/3] Preparation des fichiers...
git add .

echo [2/3] Sauvegarde avec message...
git commit -m "Mise a jour automatique - %date% %time%"

echo [3/3] Envoi vers GitHub...
git push origin main

echo.
echo ========================================
echo    DEPLOIEMENT TERMINE !
echo ========================================
echo.
echo Votre code est maintenant sur GitHub.
echo Render va automatiquement redemarrer votre agent.
echo.
pause
