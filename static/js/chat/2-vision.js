// Image descriptions from a vision model (hover or tap an image).
// Part of chat.html's script, split by concern; files load in order &
// share globals (see templates/chat.html). Config comes from CHAT_CONFIG.

// Check vision availability on load
async function initVisionCapability() {
    try {
        const response = await fetch('/vision');
        const data = await response.json();
        visionAvailable = data.available;
        visionModel = data.default;
        if (visionAvailable) {
            console.log(`Vision available: ${visionModel}`);
            setupImageHoverDescriptions();
        }
    } catch (e) {
        console.warn('Vision check failed:', e);
    }
}

// Generate a simple hash for caching
function hashString(str) {
    let hash = 0;
    for (let i = 0; i < Math.min(str.length, 1000); i++) {
        hash = ((hash << 5) - hash) + str.charCodeAt(i);
        hash |= 0;
    }
    return hash.toString();
}

// Check if URL is an external image URL
function isExternalImageUrl(src) {
    if (!src) return false;
    if (src.startsWith('data:')) return false;
    try {
        const url = new URL(src);
        return url.protocol === 'http:' || url.protocol === 'https:';
    } catch {
        return false;
    }
}

// Fetch external image via CORS proxy and convert to base64
async function fetchImageAsBase64(imageUrl) {
    const cacheKey = hashString(imageUrl);
    if (imageBase64Cache.has(cacheKey)) {
        return imageBase64Cache.get(cacheKey);
    }

    try {
        // Use CORS proxy for ethical fetching (handles robots.txt server-side)
        const proxyUrl = `${CORS_PROXY_URL}?uri_target=${encodeURIComponent(imageUrl)}`;
        const response = await fetch(proxyUrl);

        if (!response.ok) {
            if (response.status === 403) {
                console.warn(`Image blocked by robots.txt: ${imageUrl}`);
                return null;
            }
            throw new Error(`HTTP ${response.status}`);
        }

        const blob = await response.blob();

        // Verify it's actually an image
        if (!blob.type.startsWith('image/')) {
            console.warn(`Not an image: ${imageUrl} (${blob.type})`);
            return null;
        }

        // Convert to base64
        return new Promise((resolve) => {
            const reader = new FileReader();
            reader.onloadend = () => {
                const base64 = reader.result;
                imageBase64Cache.set(cacheKey, base64);
                resolve(base64);
            };
            reader.onerror = () => resolve(null);
            reader.readAsDataURL(blob);
        });
    } catch (e) {
        console.warn(`Failed to fetch image: ${imageUrl}`, e);
        return null;
    }
}

// Get base64 image data (handles both base64 and external URLs)
async function getImageBase64(imgSrc) {
    if (imgSrc.startsWith('data:image')) {
        return imgSrc;
    }
    if (isExternalImageUrl(imgSrc)) {
        return await fetchImageAsBase64(imgSrc);
    }
    return null;
}

// Fetch description for an image
async function getImageDescription(imgSrc) {
    const cacheKey = hashString(imgSrc);
    if (imageDescriptionCache.has(cacheKey)) {
        return imageDescriptionCache.get(cacheKey);
    }

    // Get base64 version of image (fetch if external)
    const base64Image = await getImageBase64(imgSrc);
    if (!base64Image) {
        return null;
    }

    try {
        const response = await fetch('/vision/describe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ image: base64Image })
        });
        const data = await response.json();
        if (data.description) {
            imageDescriptionCache.set(cacheKey, data.description);
            return data.description;
        }
    } catch (e) {
        console.warn('Failed to get image description:', e);
    }
    return null;
}

// Setup hover handlers for images in chat
function setupImageHoverDescriptions() {
    const messagesContainer = document.getElementById('chat');
    if (!messagesContainer) return;

    // Delegated: mouseover for pointers, click so a tap works on touch screens
    const describeImage = async (e) => {
        if (!visionAvailable) return;

        // Check if target is an image
        const img = e.target;
        if (img.tagName !== 'IMG') return;
        if (!img.src) return;

        // Handle both base64 and external URLs
        const isBase64 = img.src.startsWith('data:image');
        const isExternal = isExternalImageUrl(img.src);
        if (!isBase64 && !isExternal) return;

        if (img.dataset.visionProcessing || img.dataset.visionDone) return;

        // Mark as processing to avoid duplicate requests
        img.dataset.visionProcessing = 'true';
        img.style.cursor = 'wait';
        img.title = isExternal ? 'Fetching image...' : 'Generating description...';

        const description = await getImageDescription(img.src);
        if (description) {
            img.title = description;
            img.alt = description;
        } else {
            img.title = '';
        }

        img.style.cursor = '';
        delete img.dataset.visionProcessing;
        img.dataset.visionDone = 'true';
    };
    messagesContainer.addEventListener('mouseover', describeImage);
    messagesContainer.addEventListener('click', describeImage);
}
