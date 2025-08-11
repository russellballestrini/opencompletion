/**
 * Utility functions for the OpenCompletion application
 */

/**
 * Convert a string to a URL-friendly slug
 * @param {string} str - The string to slugify
 * @returns {string} - The slugified string
 */
function slugify(str) {
    return str.toLowerCase().replace(/\s+/g, '-').replace(/[^\w-]+/g, '');
}