/**
 * Utility functions for the OpenCompletion application
 */

/**
 * Convert a string to a URL-friendly slug
 * @param {string} str - The string to slugify
 * @returns {string} - The slugified string
 */
function slugify(str) {
    // Our server's rule for a new room name: [a-z0-9][a-z0-9_-]{0,63}
    return str.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9_-]+/g, '')
        .replace(/^[-_]+/, '').slice(0, 64);
}