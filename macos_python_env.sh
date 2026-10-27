# Homebrew python@3.13 pyexpat, Homebrew expat'e karşı derlenmiş; otool çıktısı
# ise /usr/lib/libexpat.1.dylib gösterir. Sistem kütüphanesinde
# _XML_SetAllocTrackerActivationThreshold yoktur. Sonuç:
# pyexpat yüklenemez, plistlib/platform.mac_ver() boş döner, pip truststore çöker.
EXPAT_LIB="/opt/homebrew/opt/expat/lib"
if [ -d "$EXPAT_LIB" ]; then
    export DYLD_LIBRARY_PATH="${EXPAT_LIB}${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
fi
