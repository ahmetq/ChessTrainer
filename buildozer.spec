# Shipit OS generated buildozer.spec
# Edit title / package.name / requirements as needed, then re-run shipit.

[app]

# (str) Title of your application
title = Chesstrainer

# (str) Package name
package.name = chesstrainer

# (str) Package domain (needed for android/ios packaging)
package.domain = org.shipit

# (str) Source code where the main.py live
source.dir = .

# (list) Source files to include
source.include_exts = py,png,jpg,jpeg,kv,atlas,ttf,txt,json,html,js,css,mp3,wav,ogg,gif,xml

# (list) Source files to exclude
source.exclude_exts = spec,pyc,pyo

# (list) List of directory to exclude
source.exclude_dirs = tests, bin, venv, .venv, .git, .github, dist, build, __pycache__

# (str) Application versioning
version = 0.1.0

# (list) Application requirements (comma separated)
requirements = python3==3.9.0,kivy,flask,python-chess,requests,pyjnius,android,werkzeug,jinja2,markupsafe

# (str) Supported orientation
orientation = portrait

# (bool) Indicate if the application should be fullscreen or not
fullscreen = 0

#------------------------------------------------------------------------------
# Android specific
#------------------------------------------------------------------------------
android.permissions = INTERNET
android.api = 33
android.minapi = 24
android.ndk = 23b
android.accept_sdk_license = True
android.archs = arm64-v8a, armeabi-v7a
android.logcat_filters = *:S python:D

# Detected entry: coach_web.py
# Buildozer looks for main.py by default. Rename your entry to main.py or adjust source.dir / your app layout.

#------------------------------------------------------------------------------
# Buildozer settings
#------------------------------------------------------------------------------

[buildozer]

log_level = 2
warn_on_root = 0
