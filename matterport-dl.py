#!/usr/bin/env python3

'''
Downloads virtual tours from matterport.
Usage is either running this program with the URL/pageid as an argument or calling the initiateDownload(URL/pageid) method.
'''

import uuid
from curl_cffi import requests
import json
import threading
import concurrent.futures
import urllib.request
from urllib.parse import urlparse
import pathlib
import re
import os
import shutil
import sys
import time
import logging
from tqdm import tqdm
from http.server import HTTPServer, SimpleHTTPRequestHandler
import decimal



# Weird hack
accessurls = []
SHOWCASE_INTERNAL_NAME = "showcase-internal.js"

def makeDirs(dirname):
    pathlib.Path(dirname).mkdir(parents=True, exist_ok=True)

def getVariants():
    variants = []
    depths = ["512", "1k", "2k", "4k"]
    for depth in range(4):
        z = depths[depth]
        for x in range(2**depth):
            for y in range(2**depth):
                for face in range(6):
                    variants.append(f"{z}_face{face}_{x}_{y}.jpg")
    return variants

def downloadUUID(accessurl, uuid):
    
    downloadFile("UUID_DAM50K", True, accessurl.format(filename=f'{uuid}_50k.dam'), f'{uuid}_50k.dam')
    shutil.copy(f'{uuid}_50k.dam', f'..{os.path.sep}{uuid}_50k.dam')
    cur_file=""
    try:
        for i in range(1000):
            cur_file=accessurl.format(filename=f'{uuid}_50k_texture_jpg_high/{uuid}_50k_{i:03d}.jpg')
            downloadFile("UUID_TEXTURE_HIGH", True, cur_file, f'{uuid}_50k_texture_jpg_high/{uuid}_50k_{i:03d}.jpg')
            cur_file=accessurl.format(filename=f'{uuid}_50k_texture_jpg_low/{uuid}_50k_{i:03d}.jpg')
            downloadFile("UUID_TEXTURE_LOW", True, cur_file, f'{uuid}_50k_texture_jpg_low/{uuid}_50k_{i:03d}.jpg')
    except Exception as ex:
        logging.warning(f'Exception downloading file: {cur_file} of: {str(ex)}')
        pass #very lazy and bad way to only download required files

def downloadSweeps(accessurl, sweeps):
    with tqdm(total=(len(sweeps)*len(getVariants()))) as pbar:
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            for sweep in sweeps:
                for variant in getVariants():
                    pbar.update(1)
                    executor.submit(downloadFile, "MODEL_SWEEPS", True, accessurl.format(filename=f'tiles/{sweep}/{variant}') + "&imageopt=1", f'tiles/{sweep}/{variant}')
                    while executor._work_queue.qsize() > 64:
                        time.sleep(0.01)

def downloadFileWithJSONPost(type, shouldExist, url, file, post_json_str, descriptor):
    global PROXY
    if "/" in file:
        makeDirs(os.path.dirname(file))
    if os.path.exists(file): #skip already downloaded files except idnex.html which is really json possibly wit hnewer access keys?
        logUrlDownloadSkipped(type, file, url, descriptor)
        return

    opener = getUrlOpener(PROXY)
    opener.addheaders.append(('Content-Type','application/json'))

    reqId = logUrlDownloadStart(type, file, url, descriptor, shouldExist)
    try:
        req = urllib.request.Request(url)

        for header in opener.addheaders: #not sure why we can't use the opener itself but it doesn't override it properly
            req.add_header(header[0],header[1])

        body_bytes = bytes(post_json_str, "utf-8")
        req.add_header('Content-Length', len(body_bytes))
        resp = urllib.request.urlopen(req, body_bytes)
        with open(file, 'w', encoding="UTF-8") as the_file:
            the_file.write(resp.read().decode("UTF-8"))
        logUrlDownloadFinish(type, file, url, descriptor, shouldExist, reqId)
    except Exception as ex:
        logUrlDownloadFinish(type, file, url, descriptor, shouldExist, reqId, ex)
        raise ex

#Add type parameter, shortResourcePath, shouldExist
def downloadFile(type, shouldExist, url, file, post_data=None):
    global accessurls, NO_TILDA_IN_PATH
    url = GetOrReplaceKey(url,False)
    # Create a session object
    session = requests.Session()

    if NO_TILDA_IN_PATH:
        file = file.replace("~","_")
    if "/" in file:
        makeDirs(os.path.dirname(file))
    if "?" in file:
        file = file.split('?')[0]

    if os.path.exists(file): #skip already downloaded files except idnex.html which is really json possibly wit hnewer access keys?
        logUrlDownloadSkipped(type, file, url, "")
        return
    reqId = logUrlDownloadStart(type, file, url, "", shouldExist)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://my.matterport.com/",
        }
        response = session.get(url, headers=headers)
        response.raise_for_status()  # Raise an exception if the response has an error status code

        with open(file, 'wb') as f:
            f.write(response.content)
        logUrlDownloadFinish(type, file, url, "", shouldExist, reqId)
        return
    except Exception as err:

        # Try again but with different accessurls (very hacky!)
        if "?t=" in url:
            for accessurl in accessurls:
                url2=""
                try:
                    url2=f"{url.split('?')[0]}?{accessurl}"
                    response = session.get(url2, headers=headers)
                    response.raise_for_status()  # Raise an exception if the response has an error status code

                    with open(file, 'wb') as f:
                        f.write(response.content)
                    logUrlDownloadFinish(type, file, url2, "", shouldExist, reqId)
                    return
                except Exception as err2:
                    logUrlDownloadFinish(type, file, url2, "", shouldExist, reqId, err2, True)
                    pass
        logUrlDownloadFinish(type, file, url, "", shouldExist, reqId, err)
        raise err
    except Exception as ex:
        logUrlDownloadFinish(type, file, url, "", shouldExist, reqId, ex)
        raise ex


def downloadGraphModels(pageid):
    global GRAPH_DATA_REQ
    makeDirs("api/mp/models")

    for key in GRAPH_DATA_REQ:
        file_path = f"api/mp/models/graph_{key}.json"
        downloadFileWithJSONPost("GRAPH_MODEL", True, "https://my.matterport.com/api/mp/models/graph",file_path, GRAPH_DATA_REQ[key], key)

requestCounter = 0
counterThreadLock = threading.Lock()
def logUrlDownloadFinish(type, localTarget, url, additionalParams, shouldExist, requestID, error=None, altUrlExists=False):
    logLevel = logging.INFO
    prefix = "Finished"
    if error:
        if altUrlExists:
            logLevel = logging.WARNING
            error = f'PartErr of: {error}'
            prefix = "aTryFail"
        else:
            logLevel = logging.ERROR
            error = f'Error of: {error}'
            prefix = "aFailure"
    else:
        error = ''
    _logUrlDownload(logLevel, prefix, type, localTarget, url, additionalParams, shouldExist, requestID, error) #not sure if should lower log elve for shouldExist  false
    
def logUrlDownloadSkipped(type, localTarget, url, additionalParams):
    _logUrlDownload(logging.DEBUG, "Skipped already downloaded", type, localTarget, url, additionalParams, False, "")
def logUrlDownloadStart(type, localTarget, url, additionalParams, shouldExist):
    global requestCounter, counterThreadLock
    ourReqId=0
    with counterThreadLock:
        requestCounter+=1
        ourReqId = requestCounter
    _logUrlDownload(logging.DEBUG, "Starting", type, localTarget, url, additionalParams, shouldExist, ourReqId)
    return ourReqId

def _logUrlDownload(logLevel, logPrefix, type, localTarget, url, additionalParams, shouldExist, requestID, optionalResult=None):
    if optionalResult:
        optionalResult = f'Result: {optionalResult}'
    else:
        optionalResult = ""

   
    logging.log(logLevel, f'{logPrefix} REQ for {type} {requestID}: should exist: {shouldExist} {optionalResult} File: {localTarget} at url: {url} {additionalParams}')


    

def downloadAssets(base):
    global BRUTE_JS_DOWNLOAD
    js_files_manual = [ #not really used any more unless we run into bad results
        "30", "46", "47", "66", "79", "134", "136", "143", "164", "250", "251", "316", "321", "356", "371", "376", "383", "386", "422", "423",
        "464", "524", "525", "539", "580", "584", "606", "614", "666", "670", "718", "721", "726", "755", "764", "828", "833", "838", "932",
         "947", "300", "309", "393", "521", "564", "633", "674", "769", "856", "934", "207","260","385","58","794","976","995", "330", "39", "519",
          "399","438", "62", "76", "926", "933"]

    language_codes = ["af", "sq", "ar-SA", "ar-IQ", "ar-EG", "ar-LY", "ar-DZ", "ar-MA", "ar-TN", "ar-OM",
     "ar-YE", "ar-SY", "ar-JO", "ar-LB", "ar-KW", "ar-AE", "ar-BH", "ar-QA", "eu", "bg",
     "be", "ca", "zh-TW", "zh-CN", "zh-HK", "zh-SG", "hr", "cs", "da", "nl", "nl-BE", "en",
     "en-US", "en-EG", "en-AU", "en-GB", "en-CA", "en-NZ", "en-IE", "en-ZA", "en-JM",
     "en-BZ", "en-TT", "et", "fo", "fa", "fi", "fr", "fr-BE", "fr-CA", "fr-CH", "fr-LU",
     "gd", "gd-IE", "de", "de-CH", "de-AT", "de-LU", "de-LI", "el", "he", "hi", "hu",
     "is", "id", "it", "it-CH", "ja", "ko", "lv", "lt", "mk", "mt", "no", "pl",
     "pt-BR", "pt", "rm", "ro", "ro-MO", "ru", "ru-MI", "sz", "sr", "sk", "sl", "sb",
     "es", "es-AR", "es-GT", "es-CR", "es-PA", "es-DO", "es-MX", "es-VE", "es-CO",
     "es-PE", "es-EC", "es-CL", "es-UY", "es-PY", "es-BO", "es-SV", "es-HN", "es-NI",
     "es-PR", "sx", "sv", "sv-FI", "th", "ts", "tn", "tr", "uk", "ur", "ve", "vi", "xh",
     "ji", "zu"]
    font_files = ["ibm-plex-sans-100", "ibm-plex-sans-100italic", "ibm-plex-sans-200", "ibm-plex-sans-200italic", "ibm-plex-sans-300",
    "ibm-plex-sans-300italic", "ibm-plex-sans-500", "ibm-plex-sans-500italic", "ibm-plex-sans-600", "ibm-plex-sans-600italic",
    "ibm-plex-sans-700", "ibm-plex-sans-700italic", "ibm-plex-sans-italic", "ibm-plex-sans-regular", "mp-font", "roboto-100", "roboto-100italic",
    "roboto-300", "roboto-300italic", "roboto-500", "roboto-500italic", "roboto-700", "roboto-700italic", "roboto-900", "roboto-900italic",
    "roboto-italic", "roboto-regular"]

    # extension assumed to be .png unless it is .svg or .jpg, for anything else place it in assets
    image_files = ["360_placement_pin_mask", "chrome", "Desktop-help-play-button.svg", "Desktop-help-spacebar", "edge", "escape", "exterior",
    "exterior_hover", "firefox", "headset-cardboard", "headset-quest", "interior", "interior_hover", "matterport-logo-light.svg",
    "mattertag-disc-128-free.v1", "mobile-help-play-button.svg", "nav_help_360", "nav_help_click_inside", "nav_help_gesture_drag",
    "nav_help_gesture_drag_two_finger", "nav_help_gesture_pinch", "nav_help_gesture_position", "nav_help_gesture_position_two_finger",
    "nav_help_gesture_tap", "nav_help_inside_key", "nav_help_keyboard_all", "nav_help_keyboard_left_right", "nav_help_keyboard_up_down",
    "nav_help_mouse_click", "nav_help_mouse_ctrl_click", "nav_help_mouse_drag_left", "nav_help_mouse_drag_right", "nav_help_mouse_position_left",
    "nav_help_mouse_position_right", "nav_help_mouse_zoom", "nav_help_tap_inside", "nav_help_zoom_keys", "NoteColor", "NoteIcon", "pinAnchor",
    "puck_256_red", "roboto-700-42_0", "safari", "scope.svg", "showcase-password-background.jpg", "surface_grid_planar_256", "tagbg", "tagmask",
                   "vert_arrows","headset-quest-2","pinIconDefault","tagColor"]

    assets = ["js/browser-check.js", "css/showcase.css", "css/unsupported_browser.css", "cursors/grab.png", "cursors/grabbing.png", "cursors/zoom-in.png",
              "cursors/zoom-out.png", "locale/strings.json", "css/ws-blur.css", "css/core.css", "css/split.css","css/late.css", "matterport-logo.svg"]
              
    # downloadFile("my.matterport.com/favicon.ico", "favicon.ico")
    file = "js/showcase.js"
    typeDict = {file: "STATIC_JS"}
    downloadFile("STATIC_ASSET", True, "https://matterport.com/nextjs-assets/images/favicon.ico", "favicon.ico") #mainly to avoid the 404
    downloadFile(typeDict[file], True, base + file, file)

    with open(file, "r", encoding="UTF-8") as f:
        showcase_cont = f.read()
    # lets try to extract the js files it might be loading and make sure we know them
    js_extracted = re.findall(r'\.e\(([0-9]{2,3})\)', showcase_cont)
    js_extracted.sort()
    for asset in assets:
        typeDict[asset] = "STATIC_ASSET"
        
    for js in js_extracted:
        file = f"js/{js}.js"
        if js not in js_files_manual and file not in assets:
#            print(f'JS FILE EXTRACTED BUT not known, please create a github issue (if one does not exist for this file) and tell us to add: {file}, did download for this run though')
            typeDict[file] = "DISCOVERED_JS"
            assets.append(file)
    if BRUTE_JS_DOWNLOAD:
        for x in range(1,1000):
            file = f"js/{x}.js"
            if file not in assets:
                typeDict[file] = "BRUTE_JS"
                assets.append(file)

    for image in image_files:
        if not image.endswith(".jpg") and not image.endswith(".svg"):
            image = image + ".png"
        file = "images/" + image
        typeDict[file] = "STATIC_IMAGE"
        assets.append(file)
    for js in js_files_manual:
        file = "js/" + js + ".js"
        typeDict[file] = "STATIC_JS"
        assets.append(file)
    for f in font_files:
        for file in ["fonts/" + f + ".woff", "fonts/" + f + ".woff2"]:
            typeDict[file] = "STATIC_FONT"
            assets.append(file)
    for lc in language_codes:
        file = "locale/messages/strings_" + lc + ".json"
        typeDict[file] = "STATIC_LOCAL_STRINGS"
        assets.append(file)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        for asset in assets:
            local_file = asset
            type = typeDict[asset]
            if local_file.endswith('/'):
                local_file = local_file    + "index.html"
            shouldExist = True
            if type.startswith("BRUTE"):
                shouldExist = False
            executor.submit(downloadFile, type, shouldExist, f"{base}{asset}", local_file)

def downloadWebglVendors(urls):
    for url in urls:      
        path= url.replace('https://static.matterport.com/','')
        downloadFile("WEBGL_FILE", False, url, path)

def setAccessURLs(pageid):
    global accessurls
    with open(f"api/player/models/{pageid}/files_type2", "r", encoding="UTF-8") as f:
        filejson = json.load(f)
        accessurls.append(filejson["base.url"].split("?")[-1])
    with open(f"api/player/models/{pageid}/files_type3", "r", encoding="UTF-8") as f:
        filejson = json.load(f)
        accessurls.append(filejson["templates"][0].split("?")[-1])


def downloadInfo(pageid):
    assets = [f"api/v1/jsonstore/model/highlights/{pageid}", f"api/v1/jsonstore/model/Labels/{pageid}", f"api/v1/jsonstore/model/mattertags/{pageid}", f"api/v1/jsonstore/model/measurements/{pageid}",
        f"api/v1/player/models/{pageid}/thumb?width=1707&dpr=1.5&disable=upscale", f"api/v1/player/models/{pageid}/", f"api/v2/models/{pageid}/sweeps", "api/v2/users/current", f"api/player/models/{pageid}/files", f"api/v1/jsonstore/model/trims/{pageid}", "api/v1/plugins?manifest=true", f"api/v1/jsonstore/model/plugins/{pageid}"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        for asset in assets:
            local_file = asset
            if local_file.endswith('/'):
                local_file = local_file    + "index.html"
            executor.submit(downloadFile, "MODEL_INFO", True, f"https://my.matterport.com/{asset}", local_file )
    makeDirs("api/mp/models")
    with open(f"api/mp/models/graph", "w", encoding="UTF-8") as f:
        f.write('{"data": "empty"}')
    for i in range(1,4): #file to url mapping
        downloadFile("FILE_TO_URL_JSON",True,f"https://my.matterport.com/api/player/models/{pageid}/files?type={i}", f"api/player/models/{pageid}/files_type{i}")
    setAccessURLs(pageid)

def downloadPics(pageid):
    with open(f"api/v1/player/models/{pageid}/index.html", "r", encoding="UTF-8") as f:
        modeldata = json.load(f)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        for image in modeldata["images"]:
            executor.submit(downloadFile, "MODEL_IMAGES", True, image["src"], urlparse(image["src"]).path[1:])

def downloadModel(pageid,accessurl):
    global ADVANCED_DOWNLOAD_ALL
    with open(f"api/v1/player/models/{pageid}/index.html", "r", encoding="UTF-8") as f:
        modeldata = json.load(f)
    accessid = re.search(r'models/([a-z0-9-_./~]*)/\{filename\}', accessurl).group(1)
    makeDirs(f"models/{accessid}")
    os.chdir(f"models/{accessid}")
    downloadUUID(accessurl,modeldata["job"]["uuid"])
    downloadSweeps(accessurl, modeldata["sweeps"])


# Patch showcase.js to fix expiration issue
def patchShowcase():
    global SHOWCASE_INTERNAL_NAME
    with open("js/showcase.js","r",encoding="UTF-8") as f:
        j = f.read()
    j = re.sub(r"\&\&\(!e.expires\|\|.{1,10}\*e.expires>Date.now\(\)\)","",j)
    j = j.replace(f'"/api/mp/','`${window.location.pathname}`+"api/mp/')
    j = j.replace("${this.baseUrl}", "${window.location.origin}${window.location.pathname}")
    j = j.replace('e.get("https://static.matterport.com/geoip/",{responseType:"json",priority:n.RequestPriority.LOW})', '{"country_code":"US","country_name":"united states","region":"CA","city":"los angeles"}')
    j = j.replace('https://static.matterport.com','')
    with open(f"js/{SHOWCASE_INTERNAL_NAME}","w",encoding="UTF-8") as f:
        f.write(j)
    j = j.replace(f'"POST"','"GET"') #no post requests for external hosted
    with open("js/showcase.js","w",encoding="UTF-8") as f:
        f.write(j)

def drange(x, y, jump):
  while x < y:
    yield float(x)
    x += decimal.Decimal(jump)
# Patch (graph_GetModelDetails.json & graph_GetSnapshots.json) URLs to Get files form local server instead of https://cdn-2.matterport.com/
def patchGetModelDetails():
    localServer = "http://127.0.0.1:8080"
    with open(f"api/mp/models/graph_GetModelDetails.json", "r", encoding="UTF-8") as f:
        j = f.read()
    j = j.replace("https://cdn-2.matterport.com", localServer)
    j = re.sub(r"validUntil\"\s:\s*\"20[\d]{2}-[\d]{2}-[\d]{2}T", "validUntil\":\"2099-01-01T", j)
    with open(f"api/mp/models/graph_GetModelDetails.json", "w", encoding="UTF-8") as f:
        f.write(j)

    with open(f"api/mp/models/graph_GetSnapshots.json", "r", encoding="UTF-8") as f:
        j = f.read()
    j = j.replace("https://cdn-2.matterport.com", localServer)
    j = re.sub(r"validUntil\"\s:\s*\"20[\d]{2}-[\d]{2}-[\d]{2}T", "validUntil\":\"2099-01-01T", j)
    with open(f"api/mp/models/graph_GetSnapshots.json", "w", encoding="UTF-8") as f:
        f.write(j)

    with open(f"api/mp/models/graph_GetModelViewPrefetch.json", "r", encoding="UTF-8") as f:
        j = f.read()
    j = j.replace("https://cdn-2.matterport.com", localServer)
    j = re.sub(r"validUntil\"\s:\s*\"20[\d]{2}-[\d]{2}-[\d]{2}T", "validUntil\":\"2099-01-01T", j)
    with open(f"api/mp/models/graph_GetModelViewPrefetch.json", "w", encoding="UTF-8") as f:
        f.write(j)



KNOWN_ACCESS_KEY=None
def GetOrReplaceKey(url, is_read_key):
    global KNOWN_ACCESS_KEY
    # key_regex = r'(t=2\-.+?\-[0-9])(&|$|")'
    key_regex = r'(t=(.+?)&k)'
    match = re.search(key_regex,url)
    if match is None:
        return url
    url_key = match.group(1)
    if KNOWN_ACCESS_KEY is None and is_read_key:
        KNOWN_ACCESS_KEY = url_key
    elif not is_read_key and KNOWN_ACCESS_KEY:
        url = url.replace(url_key, KNOWN_ACCESS_KEY)
    return url


def downloadPage(pageid):
    global ADVANCED_DOWNLOAD_ALL
    makeDirs(pageid)
    os.chdir(pageid)

    ADV_CROP_FETCH = [
            {
                "start":"width=512&crop=1024,1024,",
                "increment":'0.5'
            },
            {
                "start":"crop=512,512,",
               "increment":'0.25'
            }
        ]

    try:
        logging.basicConfig(filename='run_report.log', level=logging.DEBUG,  format='%(asctime)s %(levelname)-8s %(message)s',datefmt='%Y-%m-%d %H:%M:%S',  encoding='utf-8')
    except ValueError:
        logging.basicConfig(filename='run_report.log', level=logging.DEBUG,  format='%(asctime)s %(levelname)-8s %(message)s',datefmt='%Y-%m-%d %H:%M:%S')
    logging.debug(f'Started up a download run')
    page_root_dir = os.path.abspath('.')
    url = f"https://my.matterport.com/show/?m={pageid}"
    print(f"Downloading base page... {url}")
    try:
        r = requests.get(url)
    except Exception as error:
        if "certificate verify failed" in str(error) or "SSL certificate problem" in str(error):
            raise TypeError(f"Error: {str(error)}. Have you tried running the Install Certificates.command (or similar) file in the python folder to install the normal root certs?") from error
        else:
            raise TypeError("First request error") from error

    r.encoding = "utf-8"
    staticbase = re.search(r'<base href="(https://static.matterport.com/.*?)">', r.text).group(1)
    
    threeMin = re.search(r'https://static.matterport.com/webgl-vendors/three/[a-z0-9\-_/.]*/three.min.js', r.text).group()
    dracoWasmWrapper = threeMin.replace('three.min.js','libs/draco/gltf/draco_wasm_wrapper.js') 
    dracoDecoderWasm = threeMin.replace('three.min.js','libs/draco/gltf/draco_decoder.wasm') 
    basisTranscoderWasm = threeMin.replace('three.min.js','libs/basis/basis_transcoder.wasm') 
    basisTranscoderJs = threeMin.replace('three.min.js','libs/basis/basis_transcoder.js')
    webglVendors = [threeMin, dracoWasmWrapper, dracoDecoderWasm, basisTranscoderWasm, basisTranscoderJs ]
    
    match = re.search(r'"(https://cdn-\d*\.matterport\.com/models/[a-z0-9\-_/.]*/)([{}0-9a-z_/<>.]+)(\?t=.*?)"', r.text)
    if match:
        accessurl = f'{match.group(1)}~/{{filename}}{match.group(3)}'
        
    else:
        raise Exception(f"Can't find urls, try the main page: {url} in a browser to make sure it loads the model")

    # get a valid access key, there are a few but this is a common client used one, this also makes sure it is fresh
    file_type_content = requests.get(f"https://my.matterport.com/api/player/models/{pageid}/files?type=3") #get a valid access key, there are a few but this is a common client used one, this also makes sure it is fresh
    GetOrReplaceKey(file_type_content.text,True)
    if ADVANCED_DOWNLOAD_ALL:
        print("Doing advanced download of dollhouse/floorplan data...")
        # Started to parse the modeldata further.  As it is error prone tried to try catch silently for failures. There is more data here we could use for example:
        # queries.GetModelPrefetch.data.model.locations[X].pano.skyboxes[Y].tileUrlTemplate
        # queries.GetModelPrefetch.data.model.locations[X].pano.skyboxes[Y].urlTemplate
        # queries.GetModelPrefetch.data.model.locations[X].pano.resolutions[Y] <--- has the resolutions they offer for this one
        # goal here is to move away from some of the access url hacks, but if we are successful on try one won't matter:)
        try:
            match = re.search(r'window.MP_PREFETCHED_MODELDATA = (\{.+?\}\}\});', r.text)
            if match:
                preload_json = json.loads(match.group(1))
                base_node = preload_json["queries"]["GetModelPrefetch"]["data"]["model"]["assets"]
                for mesh in base_node["meshes"]:
                    try:
                        downloadFile("ADV_MODEL_MESH","50k" not in mesh["url"],mesh["url"], urlparse(mesh["url"]).path[1:])#not expecting the non 50k one to work but mgiht as well try
                    except:
                        pass

                # Download GetModelPrefetch.data.model.locations[X].pano.skyboxes[Y].urlTemplate
                base_node = preload_json["queries"]["GetModelPrefetch"]["data"]["model"]
                for location in base_node["locations"]:
                        for skybox in location['pano']['skyboxes']:
                            try:
                                for face in range(6):
                                    skyboxUrlTemplate = skybox['urlTemplate'].replace("<face>", f'{face}')
                                    downloadFile(skyboxUrlTemplate, urlparse(skyboxUrlTemplate).path[1:])
                            except: 
                                pass 

                # Download Tilesets
                base_node = preload_json["queries"]["GetModelPrefetch"]["data"]["model"]["assets"]
                for tileset in base_node["tilesets"]:
                            tilesetUrl = tileset['url']
                            downloadFile(tilesetUrl, urlparse(tilesetUrl).path[1:])
                            tileSet = requests.get(tilesetUrl)
                            uris = re.findall(r'"uri":"(.+?)"', tileSet.text)
                            uris.sort()
                            for uri in uris :
                                url = tileset['urlTemplate'].replace("<file>", uri)
                                downloadFile(url, urlparse(url).path[1:])
                                chunk = requests.get(url)
                                chunks = re.findall(r'(lod[0-9]_[a-zA-Z0-9-_]+\.(jpg|ktx2))', chunk.text)
                                chunks.sort()
                                try:
                                    for ktx2 in chunks:
                                        chunkUri = f"{uri[:2]}{ktx2[0]}"
                                        chunkUrl = tileset['urlTemplate'].replace("<file>", chunkUri)
                                        downloadFile(chunkUrl, urlparse(chunkUrl).path[1:])
                                except:
                                    pass
                            try:
                                for file in range(6):
                                    try:
                                        tileseUrlTemplate = tileset['urlTemplate'].replace("<file>", f'{file}.json')
                                        downloadFile(tileseUrlTemplate, urlparse(tileseUrlTemplate).path[1:])
                                        getFile = requests.get(tileseUrlTemplate)
                                        fileUris = re.findall(r'"uri":"(.*?)"', getFile.text)
                                        fileUris.sort()
                                        for fileuri in fileUris:
                                            fileUrl = tileset['urlTemplate'].replace("<file>", fileuri)
                                            downloadFile(fileUrl, urlparse(fileUrl).path[1:])


                                    except:
                                        pass
                            except: 
                                pass 
                for texture in base_node["textures"]:
                    try: #on first exception assume we have all the ones needed
                        for i in range(1000):
                            full_text_url = texture["urlTemplate"].replace("<texture>",f'{i:03d}')
                            crop_to_do = []
                            if texture["quality"] == "high":
                                crop_to_do = ADV_CROP_FETCH
                            for crop in crop_to_do:
                                for x in list(drange(0, 1, decimal.Decimal(crop["increment"]))):
                                    for y in list(drange(0, 1, decimal.Decimal(crop["increment"]))):
                                        xs = f'{x}'
                                        ys = f'{y}'
                                        if xs.endswith('.0'):
                                            xs = xs[:-2]
                                        if ys.endswith('.0'):
                                            ys = ys[:-2]
                                        complete_add=f'{crop["start"]}x{xs},y{ys}'
                                        complete_add_file = complete_add.replace("&","_")
                                        try:
                                            
                                            downloadFile("ADV_TEXTURE_CROPPED", False, full_text_url + "&" + complete_add, urlparse(full_text_url).path[1:] + complete_add_file + ".jpg") #failures here ok we dont know all teh crops that d exist
                                        except:
                                            pass

                            downloadFile("ADV_TEXTURE_FULL", True, full_text_url, urlparse(full_text_url).path[1:])
                    except:
                        pass
        except:
            pass
    # Automatic redirect if GET param isn't correct
    injectedjs = 'if (window.location.search != "?m=' + pageid + '") { document.location.search = "?m=' + pageid + '"; }'
    content = r.text.replace(staticbase,".").replace('"https://cdn-1.matterport.com/','`${window.location.origin}${window.location.pathname}` + "').replace('"https://mp-app-prod.global.ssl.fastly.net/','`${window.location.origin}${window.location.pathname}` + "').replace("window.MP_PREFETCHED_MODELDATA",f"{injectedjs};window.MP_PREFETCHED_MODELDATA").replace('"https://events.matterport.com/', '`${window.location.origin}${window.location.pathname}` + "')
    content = re.sub(r"validUntil\":\s*\"20[\d]{2}-[\d]{2}-[\d]{2}T","validUntil\":\"2099-01-01T",content)
    with open("index.html", "w", encoding="UTF-8") as f:
        f.write(content )

    print("Downloading static assets...")
    if os.path.exists("js/showcase.js"): #we want to always fetch showcase.js in case we patch it differently or the patching function starts to not work well run multiple times on itself
        os.replace("js/showcase.js","js/showcase-bk.js") #backing up existing showcase file to be safe
    downloadAssets(staticbase)
    downloadWebglVendors(webglVendors)
    # Patch showcase.js to fix expiration issue and some other changes for local hosting
    patchShowcase()
    print("Downloading model info...")
    downloadInfo(pageid)
    print("Downloading images...")
    downloadPics(pageid)
    print("Downloading graph model data...")
    downloadGraphModels(pageid)
    print(f"Patching graph_GetModelDetails.json URLs")
    patchGetModelDetails()
    print(f"Downloading model ID: {pageid} ...")
    downloadModel(pageid,accessurl)
    os.chdir(page_root_dir)
    open("api/v1/event", 'a').close()
    print("Done!")

def initiateDownload(url):
    downloadPage(getPageId(url))
def getPageId(url):
    return url.split("m=")[-1].split("&")[0]

class OurSimpleHTTPRequestHandler(SimpleHTTPRequestHandler):
    def send_error(self, code, message=None):
        if code == 404:
            logging.warning(f'404 error: {self.path} may not be downloading everything right')
        SimpleHTTPRequestHandler.send_error(self, code, message)

    def end_headers(self):
        self.send_my_headers()
        SimpleHTTPRequestHandler.end_headers(self)

    def send_my_headers(self):
        if ".js" in self.path or ".json" in self.path or ".html" in self.path:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")

    def do_GET(self):
        global SHOWCASE_INTERNAL_NAME, NO_TILDA_IN_PATH
        redirect_msg=None
        orig_request = self.path
        if NO_TILDA_IN_PATH:
            self.path = self.path.replace("~","_")

        if self.path.startswith("/js/showcase.js") and os.path.exists(f"js/{SHOWCASE_INTERNAL_NAME}"):
            redirect_msg = "using our internal showcase.js file"
            self.path = f"/js/{SHOWCASE_INTERNAL_NAME}"

        if self.path.startswith("/locale/messages/strings_") and not os.path.exists(f".{self.path}"):
            redirect_msg = "original request was for a locale we do not have downloaded"
            self.path = "/locale/strings.json"
        raw_path, _, query = self.path.partition('?')
        if "crop=" in query and raw_path.endswith(".jpg"):
            query_args = urllib.parse.parse_qs(query)
            crop_addition = query_args.get("crop", None)
            if crop_addition is not None:
                crop_addition = f'crop={crop_addition[0]}'
            else:
                crop_addition = ''

            width_addition = query_args.get("width", None)
            if width_addition is not None:
                width_addition = f'width={width_addition[0]}_'
            else:
                width_addition = ''
            test_path = raw_path + width_addition + crop_addition + ".jpg"
            if os.path.exists(f".{test_path}"):
                self.path = test_path
                redirect_msg = "dollhouse/floorplan texture request that we have downloaded, better than generic texture file"
        if redirect_msg is not None or orig_request != self.path:
            logging.info(f'Redirecting {orig_request} => {self.path} as {redirect_msg}')
        pathNoExtension=""
        if self.path.endswith(".js"):
            pathNoExtension = self.path[:-3]
        if self.path.endswith(".json"):
            pathNoExtension = self.path[:-5]
        if pathNoExtension != "":
            posFile = pathNoExtension + ".nice.js"
            if os.path.exists(f".{posFile}"):
                self.path = posFile
                logging.info(f'Redirecting {orig_request} => {self.path} as .nice.js file exists')


        SimpleHTTPRequestHandler.do_GET(self)
        return;
    def do_POST(self):
        post_msg=None
        logLevel = logging.INFO
        try:
            if urlparse(self.path).path == "/api/mp/models/graph":
                self.send_response(200)
                self.end_headers()
                content_len = int(self.headers.get('content-length'))
                post_body = self.rfile.read(content_len).decode('utf-8')
                json_body = json.loads(post_body)
                option_name = json_body["operationName"]
                if option_name in GRAPH_DATA_REQ:
                    file_path = f"api/mp/models/graph_{option_name}.json"
                    if os.path.exists(file_path):
                        with open(file_path, "r", encoding="UTF-8") as f:
                            self.wfile.write(f.read().encode('utf-8'))
                            post_msg=f"graph of operationName: {option_name} we are handling internally"
                            return;
                    else:
                        logLevel = logging.WARNING
                        post_msg=f"graph for operationName: {option_name} we don't know how to handle, but likely could add support, returning empty instead. If you get an error this may be why (include this message in bug report)."

                self.wfile.write(bytes('{"data": "empty"}', "utf-8"))
                return
        except Exception as error:
            logLevel = logging.ERROR
            post_msg = f"Error trying to handle a post request of: {str(error)} this should not happen"
            pass
        finally:
            if post_msg is not None:
                logging.log(logLevel,f'Handling a post request on {self.path}: {post_msg}')

        self.do_GET() #just treat the POST as a get otherwise:)

    def guess_type(self, path):
        res = SimpleHTTPRequestHandler.guess_type(self, path)
        if res == "text/html":
            return "text/html; charset=UTF-8"
        return res

PROXY=False
ADVANCED_DOWNLOAD_ALL=False
BRUTE_JS_DOWNLOAD=False
NO_TILDA_IN_PATH=False
GRAPH_DATA_REQ = {}

def openDirReadGraphReqs(path,pageId):
    for root, dirs, filenames in os.walk(path):
        for file in filenames:
            with open(os.path.join(root, file), "r", encoding="UTF-8") as f:
                GRAPH_DATA_REQ[file.replace(".json","")] = f.read().replace("[MATTERPORT_MODEL_ID]",pageId)

def getUrlOpener(use_proxy):
    if (use_proxy):
        proxy = urllib.request.ProxyHandler({'http': use_proxy,'https': use_proxy})
        opener = urllib.request.build_opener(proxy)
    else:
        opener = urllib.request.build_opener()
    opener.addheaders = [('User-Agent','Mozilla/5.0 (Windows NT 10.0; Win64; x64)'),('x-matterport-application-name','showcase')]
    return opener

def getCommandLineArg(name, has_value):
    for i in range(1,len(sys.argv)):
        if sys.argv[i] == name:
            sys.argv.pop(i)
            if has_value:
                return sys.argv.pop(i)
            else:
                return True
    return False

if __name__ == "__main__":
    NO_TILDA_IN_PATH = getCommandLineArg("--no-tilda", False)
    ADVANCED_DOWNLOAD_ALL = getCommandLineArg("--advanced-download", False)
    BRUTE_JS_DOWNLOAD = getCommandLineArg("--brute-js", False)
    PROXY = getCommandLineArg("--proxy", True)

    OUR_OPENER = getUrlOpener(PROXY)
    urllib.request.install_opener(OUR_OPENER)
    pageId = ""
    if len(sys.argv) > 1:
        pageId = getPageId(sys.argv[1])
    openDirReadGraphReqs("graph_posts",pageId)
    if len(sys.argv) == 2:
        initiateDownload(pageId)
    elif len(sys.argv) == 4:
        os.chdir(getPageId(pageId))
        try:
            logging.basicConfig(filename='server.log', encoding='utf-8', level=logging.DEBUG,  format='%(asctime)s %(levelname)-8s %(message)s',datefmt='%Y-%m-%d %H:%M:%S')
        except ValueError:
            logging.basicConfig(filename='server.log', level=logging.DEBUG,  format='%(asctime)s %(levelname)-8s %(message)s',datefmt='%Y-%m-%d %H:%M:%S')
        logging.info("Server started up")
        print ("View in browser: http://" + sys.argv[2] + ":" + sys.argv[3])
        httpd = HTTPServer((sys.argv[2], int(sys.argv[3])), OurSimpleHTTPRequestHandler)
        httpd.serve_forever()
    else:
        print (f"Usage:\n\tFirst Download: matterport-dl.py [url_or_page_id]\n\tThen launch the server 'matterport-dl.py [url_or_page_id] 127.0.0.1 8080' and open http://127.0.0.1:8080 in a browser\n\t--proxy 127.0.0.1:1234 -- to have it use this web proxy\n\t--advanced-download -- Use this option to try and download the cropped files for dollhouse/floorplan support\n\t--no-tilda -- Use this option to remove the tilda from file paths (say for linux)\n\t--brute-js -- Use this option to ry and download all js files 0->999 rather than just the ones detected.  Useful if you see 404 errors for js/XXX.js (where  XXX is a number)")
