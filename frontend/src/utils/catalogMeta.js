/**
 * Human-friendly metadata for astronomical object type codes and catalogs.
 */

export const OBJECT_TYPE_META = {
    'Brt Neb': {
        label: 'Bright Nebula',
        short: 'Bright Neb',
        icon: '☁️',
        description: 'Emission or reflection nebula (H II region)'
    },
    'Drk Neb': {
        label: 'Dark Nebula',
        short: 'Dark Neb',
        icon: '🌑',
        description: 'Absorption / molecular cloud dust nebula'
    },
    'Pln Neb': {
        label: 'Planetary Nebula',
        short: 'Planetary Neb',
        icon: '🫧',
        description: 'Expanding glowing shell of ionized gas'
    },
    'SN Rem': {
        label: 'Supernova Remnant',
        short: 'SN Remnant',
        icon: '💥',
        description: 'Expanding shockwave structure from a supernova'
    },
    'CL+Neb': {
        label: 'Cluster + Nebula',
        short: 'Cluster + Neb',
        icon: '✨',
        description: 'Star cluster associated with nebulosity'
    },
    'Galaxy': {
        label: 'Galaxy',
        short: 'Galaxy',
        icon: '🌀',
        description: 'Spiral, elliptical, lenticular, or irregular galaxy'
    },
    'Gal CL': {
        label: 'Galaxy Cluster',
        short: 'Galaxy Clust',
        icon: '🌌',
        description: 'Gravitationally bound group or cluster of galaxies'
    },
    'Glob CL': {
        label: 'Globular Cluster',
        short: 'Globular Clust',
        icon: '🔮',
        description: 'Dense spherical collection of ancient stars'
    },
    'Opn CL': {
        label: 'Open Cluster',
        short: 'Open Cluster',
        icon: '✦',
        description: 'Loosely bound group of young stars'
    },
    'Aster': {
        label: 'Asterism',
        short: 'Asterism',
        icon: '📐',
        description: 'Apparent star pattern in the night sky'
    },
    'Star': {
        label: 'Single Star',
        short: 'Star',
        icon: '⭐',
        description: 'Notable or variable star'
    },
    '2 Stars': {
        label: 'Double Star',
        short: 'Double Star',
        icon: '🌟',
        description: 'Visual or binary double star system'
    },
    'Quasar': {
        label: 'Quasar / AGN',
        short: 'Quasar',
        icon: '⚡',
        description: 'Quasi-stellar object / active galactic nucleus'
    },
    'Neb+Gx': {
        label: 'Nebula + Galaxy',
        short: 'Neb + Galaxy',
        icon: '🪐',
        description: 'Compound field containing nebula and galaxy'
    },
    'Glo+Gx': {
        label: 'Globular + Galaxy',
        short: 'Glob + Galaxy',
        icon: '🔭',
        description: 'Globular cluster associated with an external galaxy'
    },
    'C+N+G': {
        label: 'Cluster + Neb + Gx',
        short: 'Clust+Neb+Gx',
        icon: '🌠',
        description: 'Composite field with cluster, nebula, and galaxy'
    }
};

export const CATALOG_META = {
    messier: {
        label: 'Messier',
        code: 'M',
        subtitle: '110 Classic Deep-Sky Showcase Targets',
        category: 'Core'
    },
    ngc: {
        label: 'NGC',
        code: 'NGC',
        subtitle: 'New General Catalogue of Nebulae & Clusters',
        category: 'Core'
    },
    ic: {
        label: 'IC',
        code: 'IC',
        subtitle: 'Index Catalogue (Supplement to NGC)',
        category: 'Core'
    },
    sharpless: {
        label: 'Sharpless',
        code: 'Sh2',
        subtitle: 'H II Emission Nebulae Catalog',
        category: 'Nebulae'
    },
    barnard: {
        label: 'Barnard',
        code: 'B',
        subtitle: 'Dark Markings & Absorption Nebulae',
        category: 'Nebulae'
    },
    pk: {
        label: 'Perek–Kohoutek',
        code: 'PK',
        subtitle: 'Galactic Planetary Nebulae',
        category: 'Nebulae'
    },
    abell: {
        label: 'Abell',
        code: 'Abell',
        subtitle: 'Planetary Nebulae & Rich Galaxy Clusters',
        category: 'Nebulae & Galaxies'
    },
    chandra_snr: {
        label: 'Chandra SNR',
        code: 'SNR',
        subtitle: 'Galactic Supernova Remnants',
        category: 'Nebulae'
    },
    collinder: {
        label: 'Collinder',
        code: 'Cr',
        subtitle: 'Open Star Clusters & Associations',
        category: 'Clusters'
    },
    berkeley: {
        label: 'Berkeley',
        code: 'Be',
        subtitle: 'Distant Open Star Clusters',
        category: 'Clusters'
    },
    ruprecht: {
        label: 'Ruprecht',
        code: 'Ru',
        subtitle: 'Southern & Milky Way Open Clusters',
        category: 'Clusters'
    },
    ugc: {
        label: 'Uppsala (UGC)',
        code: 'UGC',
        subtitle: 'Northern Hemisphere Galaxies',
        category: 'Galaxies'
    },
    others: {
        label: 'Other Catalogs',
        code: 'Misc',
        subtitle: 'Melotte, Stock, Trumpler, van den Bergh & more',
        category: 'Mixed'
    }
};

export function getObjectTypeLabel(typeCode) {
    if (!typeCode) return 'Unknown';
    return OBJECT_TYPE_META[typeCode]?.label || typeCode;
}

export function getObjectTypeShort(typeCode) {
    if (!typeCode) return '';
    return OBJECT_TYPE_META[typeCode]?.short || typeCode;
}

export function getObjectTypeIcon(typeCode) {
    if (!typeCode) return '✦';
    return OBJECT_TYPE_META[typeCode]?.icon || '✦';
}

export function getObjectTypeDescription(typeCode) {
    if (!typeCode) return '';
    return OBJECT_TYPE_META[typeCode]?.description || typeCode;
}

export function getCatalogMeta(catalogKey) {
    if (!catalogKey) return { label: '', code: '', subtitle: '' };
    const key = catalogKey.toLowerCase();
    return CATALOG_META[key] || {
        label: catalogKey.toUpperCase(),
        code: catalogKey.toUpperCase(),
        subtitle: 'Astronomical Object Catalog',
        category: 'Custom'
    };
}

export function formatMagnitude(mag) {
    if (mag === undefined || mag === null || mag === '' || Number(mag) >= 99) {
        return 'N/A';
    }
    const num = Number(mag);
    return Number.isFinite(num) ? num.toFixed(1) : String(mag);
}
