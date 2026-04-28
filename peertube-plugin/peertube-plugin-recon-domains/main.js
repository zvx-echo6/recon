async function register ({ videoCategoryManager }) {
  const reconDomains = {
    100: 'Agriculture & Livestock',
    101: 'Civil Organization',
    102: 'Communications',
    103: 'Food Systems',
    104: 'Foundational Skills',
    105: 'Logistics',
    106: 'Medical',
    107: 'Navigation',
    108: 'Operations',
    109: 'Power Systems',
    110: 'Preservation & Storage',
    111: 'Security',
    112: 'Shelter & Construction',
    113: 'Technology',
    114: 'Tools & Equipment',
    115: 'Vehicles',
    116: 'Water Systems',
    117: 'Wilderness Skills'
  }

  for (const [id, label] of Object.entries(reconDomains)) {
    videoCategoryManager.addConstant(parseInt(id), label)
  }
}

module.exports = { register }
