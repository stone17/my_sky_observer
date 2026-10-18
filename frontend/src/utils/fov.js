/**
 * Single Source of Truth for FOV calculations in the frontend.
 * Matches backend AstroCalculator.calculate_fov and stream_service.compute_fov_and_setup.
 */

export const MIN_IMAGE_FOV_DEG = 0.25;
export const DEFAULT_IMAGE_PADDING = 1.15;

/**
 * Compute physical sensor width and height FOV in degrees.
 * Formula: 2 * atan(sensor_dim / (2 * focal_length)) * (180 / PI)
 */
export function computeSensorFov(telescope, camera) {
    const fl = Number(telescope?.focal_length);
    const sw = Number(camera?.sensor_width);
    const sh = Number(camera?.sensor_height);

    if (!fl || fl <= 0 || !sw || sw <= 0 || !sh || sh <= 0) {
        return { w: 0, h: 0 };
    }

    const w = 2 * Math.atan(sw / (2 * fl)) * (180 / Math.PI);
    const h = 2 * Math.atan(sh / (2 * fl)) * (180 / Math.PI);
    return { w, h };
}

/**
 * Compute default sky survey image FOV in degrees from equipment and padding.
 * Matches backend: max(max(fov_w_deg, fov_h_deg) * image_padding, 0.25)
 */
export function computeDefaultImageFov(telescope, camera, imagePadding = DEFAULT_IMAGE_PADDING) {
    const { w, h } = computeSensorFov(telescope, camera);
    if (w <= 0 && h <= 0) {
        return 1.0;
    }
    const pad = Number(imagePadding) > 0 ? Number(imagePadding) : DEFAULT_IMAGE_PADDING;
    const rawFov = Math.max(Math.max(w, h) * pad, MIN_IMAGE_FOV_DEG);
    return parseFloat(rawFov.toFixed(2));
}

/**
 * Clamp and round any image FOV value (in degrees) to valid bounds.
 */
export function normalizeImageFov(fov, fallbackFov = 1.0) {
    const num = Number(fov);
    if (!Number.isFinite(num) || num <= 0) {
        return parseFloat(Math.max(Number(fallbackFov) || 1.0, MIN_IMAGE_FOV_DEG).toFixed(2));
    }
    return parseFloat(Math.max(num, MIN_IMAGE_FOV_DEG).toFixed(2));
}
