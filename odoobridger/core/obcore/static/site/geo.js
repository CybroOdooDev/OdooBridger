/* Where the visitor is, worked out from the browser (no permission prompt):
 * the time zone gives the country, the country gives the phone code. */

// [ISO code, name, dialling code]
export const COUNTRIES = [["AF","Afghanistan","93"],["AL","Albania","355"],["DZ","Algeria","213"],["AS","American Samoa","1684"],["AD","Andorra","376"],["AO","Angola","244"],["AI","Anguilla","1264"],["AG","Antigua and Barbuda","1268"],["AR","Argentina","54"],["AM","Armenia","374"],["AW","Aruba","297"],["AU","Australia","61"],["AT","Austria","43"],["AZ","Azerbaijan","994"],["BS","Bahamas","1242"],["BH","Bahrain","973"],["BD","Bangladesh","880"],["BB","Barbados","1246"],["BY","Belarus","375"],["BE","Belgium","32"],["BZ","Belize","501"],["BJ","Benin","229"],["BM","Bermuda","1441"],["BT","Bhutan","975"],["BO","Bolivia","591"],["BA","Bosnia and Herzegovina","387"],["BW","Botswana","267"],["BR","Brazil","55"],["IO","British Indian Ocean Territory","246"],["VG","British Virgin Islands","1284"],["BN","Brunei","673"],["BG","Bulgaria","359"],["BF","Burkina Faso","226"],["BI","Burundi","257"],["KH","Cambodia","855"],["CM","Cameroon","237"],["CA","Canada","1"],["CV","Cape Verde","238"],["KY","Cayman Islands","1345"],["CF","Central African Republic","236"],["TD","Chad","235"],["CL","Chile","56"],["CN","China","86"],["CO","Colombia","57"],["KM","Comoros","269"],["CG","Congo","242"],["CD","Congo (DRC)","243"],["CK","Cook Islands","682"],["CR","Costa Rica","506"],["CI","Côte d'Ivoire","225"],["HR","Croatia","385"],["CU","Cuba","53"],["CW","Curaçao","599"],["CY","Cyprus","357"],["CZ","Czechia","420"],["DK","Denmark","45"],["DJ","Djibouti","253"],["DM","Dominica","1767"],["DO","Dominican Republic","1809"],["EC","Ecuador","593"],["EG","Egypt","20"],["SV","El Salvador","503"],["GQ","Equatorial Guinea","240"],["ER","Eritrea","291"],["EE","Estonia","372"],["SZ","Eswatini","268"],["ET","Ethiopia","251"],["FK","Falkland Islands","500"],["FO","Faroe Islands","298"],["FJ","Fiji","679"],["FI","Finland","358"],["FR","France","33"],["GF","French Guiana","594"],["PF","French Polynesia","689"],["GA","Gabon","241"],["GM","Gambia","220"],["GE","Georgia","995"],["DE","Germany","49"],["GH","Ghana","233"],["GI","Gibraltar","350"],["GR","Greece","30"],["GL","Greenland","299"],["GD","Grenada","1473"],["GP","Guadeloupe","590"],["GU","Guam","1671"],["GT","Guatemala","502"],["GG","Guernsey","44"],["GN","Guinea","224"],["GW","Guinea-Bissau","245"],["GY","Guyana","592"],["HT","Haiti","509"],["HN","Honduras","504"],["HK","Hong Kong","852"],["HU","Hungary","36"],["IS","Iceland","354"],["IN","India","91"],["ID","Indonesia","62"],["IR","Iran","98"],["IQ","Iraq","964"],["IE","Ireland","353"],["IM","Isle of Man","44"],["IL","Israel","972"],["IT","Italy","39"],["JM","Jamaica","1876"],["JP","Japan","81"],["JE","Jersey","44"],["JO","Jordan","962"],["KZ","Kazakhstan","7"],["KE","Kenya","254"],["KI","Kiribati","686"],["XK","Kosovo","383"],["KW","Kuwait","965"],["KG","Kyrgyzstan","996"],["LA","Laos","856"],["LV","Latvia","371"],["LB","Lebanon","961"],["LS","Lesotho","266"],["LR","Liberia","231"],["LY","Libya","218"],["LI","Liechtenstein","423"],["LT","Lithuania","370"],["LU","Luxembourg","352"],["MO","Macao","853"],["MG","Madagascar","261"],["MW","Malawi","265"],["MY","Malaysia","60"],["MV","Maldives","960"],["ML","Mali","223"],["MT","Malta","356"],["MH","Marshall Islands","692"],["MQ","Martinique","596"],["MR","Mauritania","222"],["MU","Mauritius","230"],["YT","Mayotte","262"],["MX","Mexico","52"],["FM","Micronesia","691"],["MD","Moldova","373"],["MC","Monaco","377"],["MN","Mongolia","976"],["ME","Montenegro","382"],["MS","Montserrat","1664"],["MA","Morocco","212"],["MZ","Mozambique","258"],["MM","Myanmar","95"],["NA","Namibia","264"],["NR","Nauru","674"],["NP","Nepal","977"],["NL","Netherlands","31"],["NC","New Caledonia","687"],["NZ","New Zealand","64"],["NI","Nicaragua","505"],["NE","Niger","227"],["NG","Nigeria","234"],["NU","Niue","683"],["KP","North Korea","850"],["MK","North Macedonia","389"],["MP","Northern Mariana Islands","1670"],["NO","Norway","47"],["OM","Oman","968"],["PK","Pakistan","92"],["PW","Palau","680"],["PS","Palestine","970"],["PA","Panama","507"],["PG","Papua New Guinea","675"],["PY","Paraguay","595"],["PE","Peru","51"],["PH","Philippines","63"],["PL","Poland","48"],["PT","Portugal","351"],["PR","Puerto Rico","1787"],["QA","Qatar","974"],["RE","Réunion","262"],["RO","Romania","40"],["RU","Russia","7"],["RW","Rwanda","250"],["WS","Samoa","685"],["SM","San Marino","378"],["ST","São Tomé and Príncipe","239"],["SA","Saudi Arabia","966"],["SN","Senegal","221"],["RS","Serbia","381"],["SC","Seychelles","248"],["SL","Sierra Leone","232"],["SG","Singapore","65"],["SX","Sint Maarten","1721"],["SK","Slovakia","421"],["SI","Slovenia","386"],["SB","Solomon Islands","677"],["SO","Somalia","252"],["ZA","South Africa","27"],["KR","South Korea","82"],["SS","South Sudan","211"],["ES","Spain","34"],["LK","Sri Lanka","94"],["KN","St Kitts and Nevis","1869"],["LC","St Lucia","1758"],["VC","St Vincent and the Grenadines","1784"],["SD","Sudan","249"],["SR","Suriname","597"],["SE","Sweden","46"],["CH","Switzerland","41"],["SY","Syria","963"],["TW","Taiwan","886"],["TJ","Tajikistan","992"],["TZ","Tanzania","255"],["TH","Thailand","66"],["TL","Timor-Leste","670"],["TG","Togo","228"],["TK","Tokelau","690"],["TO","Tonga","676"],["TT","Trinidad and Tobago","1868"],["TN","Tunisia","216"],["TR","Türkiye","90"],["TM","Turkmenistan","993"],["TC","Turks and Caicos Islands","1649"],["TV","Tuvalu","688"],["UG","Uganda","256"],["UA","Ukraine","380"],["AE","United Arab Emirates","971"],["GB","United Kingdom","44"],["US","United States","1"],["UY","Uruguay","598"],["UZ","Uzbekistan","998"],["VU","Vanuatu","678"],["VA","Vatican City","379"],["VE","Venezuela","58"],["VN","Vietnam","84"],["VI","US Virgin Islands","1340"],["WF","Wallis and Futuna","681"],["EH","Western Sahara","212"],["YE","Yemen","967"],["ZM","Zambia","260"],["ZW","Zimbabwe","263"]];

export const flag = (iso) => (iso.length === 2 && iso !== 'XK'
  ? String.fromCodePoint(...[...iso].map((c) => 0x1f1a5 + c.charCodeAt(0))) : '🏳️');

const ZONE_COUNTRY = {
  'Asia/Kolkata': 'IN', 'Asia/Calcutta': 'IN', 'Asia/Dubai': 'AE', 'Asia/Riyadh': 'SA', 'Asia/Qatar': 'QA',
  'Asia/Kuwait': 'KW', 'Asia/Muscat': 'OM', 'Asia/Bahrain': 'BH', 'Asia/Karachi': 'PK', 'Asia/Dhaka': 'BD',
  'Asia/Colombo': 'LK', 'Asia/Kathmandu': 'NP', 'Asia/Katmandu': 'NP', 'Asia/Singapore': 'SG',
  'Asia/Kuala_Lumpur': 'MY', 'Asia/Tokyo': 'JP', 'Asia/Seoul': 'KR', 'Asia/Shanghai': 'CN', 'Asia/Hong_Kong': 'HK',
  'Asia/Bangkok': 'TH', 'Asia/Jakarta': 'ID', 'Asia/Manila': 'PH', 'Asia/Ho_Chi_Minh': 'VN', 'Asia/Saigon': 'VN',
  'Asia/Tehran': 'IR', 'Asia/Baghdad': 'IQ', 'Asia/Amman': 'JO', 'Asia/Beirut': 'LB', 'Asia/Jerusalem': 'IL',
  'Asia/Kabul': 'AF', 'Asia/Tashkent': 'UZ', 'Asia/Almaty': 'KZ', 'Asia/Taipei': 'TW', 'Asia/Yangon': 'MM',
  'Asia/Phnom_Penh': 'KH', 'Asia/Thimphu': 'BT', 'Indian/Maldives': 'MV', 'Indian/Mauritius': 'MU',
  'Pacific/Auckland': 'NZ', 'Pacific/Fiji': 'FJ', 'Europe/London': 'GB', 'Europe/Berlin': 'DE',
  'Europe/Paris': 'FR', 'Europe/Amsterdam': 'NL', 'Europe/Madrid': 'ES', 'Europe/Rome': 'IT', 'Europe/Istanbul': 'TR',
  'Europe/Moscow': 'RU', 'Europe/Zurich': 'CH', 'Europe/Vienna': 'AT', 'Europe/Brussels': 'BE', 'Europe/Stockholm': 'SE',
  'Europe/Oslo': 'NO', 'Europe/Copenhagen': 'DK', 'Europe/Helsinki': 'FI', 'Europe/Dublin': 'IE', 'Europe/Lisbon': 'PT',
  'Europe/Athens': 'GR', 'Europe/Warsaw': 'PL', 'Europe/Prague': 'CZ', 'Europe/Budapest': 'HU', 'Europe/Bucharest': 'RO',
  'Europe/Kiev': 'UA', 'Europe/Kyiv': 'UA', 'Europe/Sofia': 'BG', 'Europe/Belgrade': 'RS', 'Europe/Zagreb': 'HR',
  'Europe/Luxembourg': 'LU', 'Europe/Reykjavik': 'IS', 'America/Toronto': 'CA', 'America/Vancouver': 'CA',
  'America/Edmonton': 'CA', 'America/Winnipeg': 'CA', 'America/Halifax': 'CA', 'America/Mexico_City': 'MX',
  'America/Sao_Paulo': 'BR', 'America/Argentina/Buenos_Aires': 'AR', 'America/Buenos_Aires': 'AR',
  'America/Bogota': 'CO', 'America/Lima': 'PE', 'America/Santiago': 'CL', 'America/Caracas': 'VE',
  'America/Montevideo': 'UY', 'America/Panama': 'PA', 'America/Costa_Rica': 'CR', 'America/Jamaica': 'JM',
  'Africa/Johannesburg': 'ZA', 'Africa/Nairobi': 'KE', 'Africa/Lagos': 'NG', 'Africa/Cairo': 'EG',
  'Africa/Casablanca': 'MA', 'Africa/Algiers': 'DZ', 'Africa/Tunis': 'TN', 'Africa/Addis_Ababa': 'ET',
  'Africa/Accra': 'GH', 'Africa/Dar_es_Salaam': 'TZ', 'Africa/Kampala': 'UG', 'Africa/Khartoum': 'SD',
};

export function detectZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; }
}

export function detectCountry() {
  const zone = detectZone();
  if (ZONE_COUNTRY[zone]) return ZONE_COUNTRY[zone];
  if (zone.startsWith('Australia/')) return 'AU';
  if (zone.startsWith('America/') || zone.startsWith('US/') || zone === 'Pacific/Honolulu') return 'US';
  const region = (navigator.language || '').split('-')[1];
  return region ? region.toUpperCase() : null;
}

/** The visitor's country entry; India when it cannot be told. */
export function detectCountryEntry() {
  const iso = detectCountry();
  return COUNTRIES.find((c) => c[0] === iso) || COUNTRIES.find((c) => c[0] === 'IN');
}
