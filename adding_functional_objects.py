import argparse
import psycopg2
import pandas as pd, json
import os, time, random
import traceback
from typing import Dict, Iterable, Tuple, List, Any, Union, Optional, Callable, Set
from database_properties import Properties

properties: Properties

def ensure_tables(conn: psycopg2.extensions.connection, object_class: str, commit: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(f'CREATE TABLE IF NOT EXISTS {object_class}_object_types ('
                '   id serial PRIMARY KEY NOT NULL,'
                '   name VARCHAR(80) UNIQUE NOT NULL,'
                '   code VARCHAR(40) UNIQUE NOT NULL'
                ')')
        cur.execute(f'CREATE TABLE IF NOT EXISTS {object_class}_objects ('
                '   id serial PRIMARY KEY NOT NULL,'
                '   functional_object_id integer REFERENCES functional_objects(id) NOT NULL,'
                f'  type_id integer REFERENCES {object_class}_object_types(id) NOT NULL,'
                '   properties jsonb'
                ')')
        if commit:
            conn.commit()

def ensure_service(conn: psycopg2.extensions.connection, service_type: str, service_code: Optional[str],
        capacity_min: Optional[int], capacity_max: Optional[int], city_function: Union[int, str], commit: bool = True) -> int:
    with conn.cursor() as cur:
        # AND code = %s AND capacity_min = %s AND capacity_max = %s AND city_function = ' + 
        #        ('%s' if isinstance(city_function, int) else '(SELECT id from city_functions WHERE name = %s)'),
        cur.execute('SELECT id FROM service_types WHERE name = %s', (service_type,))
        res = cur.fetchone()
        if res is not None:
            return res[0]
        if service_code is None or city_function is None:
            raise ValueError(f'service type "{service_type}" is not found in the database, but code and/or city_function is not provided')
        if capacity_min is None or capacity_max is None:
            print(f'service type "{service_type}" is not found in the database and will be inserted, but min_capacity and/or max_capacity is not provided.')
        cur.execute('INSERT INTO service_types (name, code, capacity_min, capacity_max, city_function_id) VALUES (%s, %s, %s, %s, ' +
                ('%s' if isinstance(city_function, int) else '(SELECT id from city_functions WHERE name = %s or code = %s)') +
                ') RETURNING ID', (service_type, service_code, capacity_min, capacity_max, city_function, city_function))
        if commit:
            conn.commit()
        return cur.fetchone()[0]

def get_type_name(row_or_name: Union[str, pd.Series], amenity_field: Optional[str] = 'amenity') -> str:
    '''get_type_name takes name or concrete functional object Series and returns its type name in lowercase
    
    If string name is given in input, it is returned in lowercase

    If Series is given in input, if there is `type` column in it, its value returned, otherwise `amenity` column value is used
    '''
    if isinstance(row_or_name, pd.Series):
        if row_or_name.get(amenity_field):
            return row_or_name[amenity_field].lower()
        else:
            raise ValueError(f'get_type_name error: no {amenity_field} found in entity. You need to set default value')
    return row_or_name.lower() # type: ignore

_type_ids: Dict[Tuple[str, str], int] = {}
def get_type_id(cur: psycopg2.extensions.connection, object_class: str, row_or_name: Union[str, pd.Series],
        object_types: Dict[str, Tuple[str, str]], amenity_field: Optional[str] = 'amenity', print_errors: bool = False) -> int:
    '''get_type_id is a function which returns the id of object_class type with name=`name`, and if it is missing
    in the database, then inserts it with code=`code`.
    '''
    name = get_type_name(row_or_name, amenity_field)
    if not name in object_types:
        if print_errors:
            print(f'{object_class} object type "{name}" is not in the list, ignoring')
        raise ValueError(f'{object_class} object type "{name}" is not in the list, ignoring')
    name, code = object_types[name]
    table_name = f'{object_class}_object_types'
    global _type_ids
    if (object_class, name) in _type_ids:
        return _type_ids[(object_class, name)]
    cur.execute(f'SELECT id FROM {table_name} WHERE name = %s', (name,))
    res = cur.fetchone()
    if res is None:
        cur.execute(f'INSERT INTO {table_name} (name, code) VALUES (%s, %s) RETURNING id', (name, code))
        _type_ids[(object_class, name)] = cur.fetchone()[0]
        return _type_ids[(object_class, name)]
    _type_ids[(object_class, name)] = res[0]
    return _type_ids[(object_class, name)]

def insert_object(conn: psycopg2.extensions.connection, row: pd.Series, phys_id: int, object_class: str,
        service_type_id: int, type_id: int, mapping: Dict[str, str] = {'name': 'name',
                'opening_hours': 'opening_hours', 'website': 'contact:website', 'phone': 'contact:phone'},
        commit: bool = True) -> int:
    '''insert_object inserts functional_object with connection to physical_object with phys_id and concrete <service_type>_object connected to it.
    
    Returns functional object id inserted
    '''
    with conn.cursor() as cur:
        cur.execute('SELECT st.capacity_min, st.capacity_max, cf.id, st.id, it.id FROM infrastructure_types it JOIN city_functions cf ON cf.infrastructure_type_id = it.id JOIN'
                '               service_types st ON st.city_function_id = cf.id WHERE st.id = %s', (service_type_id,))
        mn, mx, *ids = cur.fetchone()
        cur.execute('INSERT INTO functional_objects (name, opening_hours, website, phone, city_function_id, service_type_id, infrastructure_type_id, capacity)'
                ' VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                    (row.get(mapping['name']) or '(не указано)', row.get(mapping['opening_hours']), row.get(mapping['website']),
                            row.get(mapping['phone']), *ids, (random.randint(mn, mx) if mn is not None and mx is not None else None)))
        func_id = cur.fetchone()[0]
        cur.execute('INSERT INTO phys_objs_fun_objs (phys_obj_id, fun_obj_id) VALUES (%s, %s)', (phys_id, func_id))
        cur.execute(f'INSERT INTO {object_class}_objects (functional_object_id, type_id) VALUES (%s, %s)', (func_id, type_id))
        if commit:
            conn.commit()
        return func_id

def add_objects(conn: psycopg2.extensions.connection, objects: pd.DataFrame,
        object_class: str, object_types: Dict[str, Tuple[str, str]], service_type_id: int,
        amenity: Optional[str] = None, mapping: Dict[str, str] = {'amenity': 'amenity', 'name': 'name', 'opening_hours': 'opening_hours', 
                'website': 'contact:website', 'phone': 'contact:phone', 'address': 'yand_adr',
                'osm_id': 'id', 'lat': 'x', 'lng': 'y'}, address_prefixes: List[str] = ['Россия, Санкт-Петербург'],
        commit: bool = True, verbose: bool = False) -> pd.DataFrame:
    '''add_objects inserts objects to database.

    Input:

        - `conn` - connection for the database
        - `objects` - DataFrame containing objects. 
        - `object_class` - name of table containing concrete functional objects ("catering" for catering_objects, "culture" for culture_objects, etc)
        - `object_types` - link from amenity to .._object_type[name, code]
        - `service_type_id` - id of service_type (got by `ensure_service` function)
        - `amenity` - name of concrete functional object type if needed to overrite amenity/type from `objects`
        - `mapping` - map of namings in the database as keys and namings in the objects as values
        - `address_prefixes` - list of possible prefixes (will be sorted by length)
        - `commit` - True to commit changes, False for dry run, only resulting DataFrame is returned
        - `verbose` - True to output traceback with errors, False for only error messages printing

    Return:

        - dataframe of objects with "result" column added and "functional_obj_id" columns added

    Algorithm steps:

        1. Check if building with given address is present in the database

        2. If found:

            2.1. If functional object with the same name connected to the same physical object is present, skip

            2.2. Else get building id and physical_object id

        3. Else:
            
            3.1. If address does not start with regular prefix, skip

            3.2. If there is building which geometry contains current object's coordinates, get its building id and physical_object id

            3.3. Else insert temporary object with Point as geometry and get its building id and physical_object id

        5. Insert functional_object connected to physical_object and concrete functional object for it by calling `insert_object`
    '''
    objects = objects.drop(objects[objects[mapping['address']].isna()].index)
    objects[mapping['address']] = objects[mapping['address']].apply(lambda x: x.replace('?', '').strip())
    present = 0 # objects already present in the database
    added_to_building_adr, added_to_building_geom, added_as_points, skipped = 0, 0, 0, 0
    results: List[str] = list(('',) * objects.shape[0])
    functional_ids: List[int] = [-1 for _ in range(objects.shape[0])]
    address_prefixes = sorted(address_prefixes, key=lambda s: -len(s))
    with conn.cursor() as cur:
        for i, (_, row) in enumerate(objects.iterrows()):
            try:
                row[mapping['lat']] = round(row[mapping['lat']], 6)
                row[mapping['lng']] = round(row[mapping['lng']], 6)
                for address_prefix in address_prefixes:
                    if row.get(mapping['address'], '').startswith(address_prefix):
                        break
                else:
                    if len(address_prefixes) == 1:
                        results[i] = f'Skipped (address does not start with "{address_prefixes[0]}" prefix)'
                    else:
                        results[i] = f'Skipped (address does not start with any of {len(address_prefixes)} prefixes)'
                    skipped += 1
                    continue
                if sum(map(lambda x: bool(row.get(x)), (mapping['address'], mapping['lat'], mapping['lng']))) != 3:
                    results[i] = f'Skipped (missing one of the required fields: {mapping["address"]}, {mapping["lat"]}, {mapping["lng"]})'
                    skipped += 1
                    continue
                name = row.get(mapping['name']) or '(не указано)'
                try:
                    type_id = get_type_id(cur, object_class, amenity or row, object_types, mapping.get('amenity'), verbose)
                    if commit:
                        conn.commit()
                except ValueError:
                    results[i] = f'Skipped (type "{amenity or row.get(mapping.get("amenity")) or "(unknown)"}" is missing in types list)'
                    skipped += 1
                    continue
                phys_id: int
                build_id: int
                cur.execute('SELECT phys.id, build.id FROM physical_objects phys JOIN buildings build on build.physical_object_id = phys.id'
                        ' WHERE build.address = %s AND ST_Distance(phys.center::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) < 200 LIMIT 1',
                            (row.get(mapping['address'])[len(address_prefix):].strip(', '), row[mapping['lng']], row[mapping['lat']]))
                res = cur.fetchone()
                if res is not None: # if building with the same address found and distance between point and the center of geometry is less than 200m
                    phys_id, build_id = res
                    cur.execute('SELECT func.id FROM phys_objs_fun_objs pf'
                                ' JOIN functional_objects func ON pf.fun_obj_id = func.id'
                                f' JOIN {object_class}_objects concrete_func ON func.id = concrete_func.functional_object_id'
                                ' WHERE pf.phys_obj_id = %s AND func.name = %s AND concrete_func.type_id = %s LIMIT 1', (phys_id, name, type_id))
                    res = cur.fetchone()
                    if res is not None:
                        present += 1
                        results[i] = f'Service presented fully as functional_object (build_id = {build_id}, phys_id = {phys_id}, functional_object_id = {res[0]})'
                        functional_ids[i] = res[0]
                        continue
                    added_to_building_adr += 1
                    results[i] = f'Building found by address (build_id = {build_id}, phys_id = {phys_id})'
                else: # if no building with the same address found or distance is too high (address is wrong or it's not a concrete house)
                    cur.execute('SELECT build.id, phys.id, build.address FROM buildings build'
                            ' JOIN physical_objects phys ON build.physical_object_id = phys.id'
                            ' WHERE ST_Within(ST_SetSRID(ST_MakePoint(%s, %s), 4326), phys.geometry)',
                            (row[mapping['lng']], row[mapping['lat']]))
                    res = cur.fetchone()
                    if res is not None: # if found inside other building geometry
                        build_id, phys_id, address = res
                        cur.execute('SELECT func.id FROM phys_objs_fun_objs pf'
                                ' JOIN functional_objects func ON pf.fun_obj_id = func.id'
                                f' JOIN {object_class}_objects concrete_func ON func.id = concrete_func.functional_object_id'
                                ' WHERE pf.phys_obj_id = %s AND func.name = %s AND concrete_func.type_id = %s LIMIT 1', (phys_id, name, type_id))
                        res = cur.fetchone()
                        if res is not None:
                            present += 1
                            results[i] = f'Service presented fully as functional_object with different address: "{address}" (build_id = {build_id}, phys_id = {phys_id}, functional_object_id = {res[0]})'
                            functional_ids[i] = res[0]
                            continue
                        added_to_building_geom += 1
                        results[i] = f'Service added inside the geometry of "{address}" (build_id = {build_id}, phys_id = {phys_id})'
                    else: # if no address nor existing geometry found - insert as temporary point
                        cur.execute('INSERT INTO physical_objects (osm_id, pollution_category_id, geometry, center, description) VALUES'
                                ' (%s, (SELECT id FROM pollution_categories WHERE code = \'zero\'), ST_SetSRID(ST_MakePoint(%s, %s), 4326), ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s) RETURNING id',
                                (row.get(mapping['osm_id']), row[mapping['lng']], row[mapping['lat']], row[mapping['lng']], row[mapping['lat']],
                                f'{row.get(mapping["name"])} (временное)' if row.get(mapping['name']) else 'здание (временное)'))
                        phys_id = cur.fetchone()[0]
                        cur.execute('INSERT INTO buildings (physical_object_id, address, is_temp) VALUES (%s, %s, \'true\') RETURNING id',
                                    (phys_id, row.get(mapping['address'])[len(address_prefix):].strip(', ')))
                        build_id = cur.fetchone()[0]
                        added_as_points += 1
                        results[i] = f'Building inserted with Point type as temporary building (build_id = {build_id}, phys_id = {phys_id})'
                functional_ids[i] = insert_object(conn, row, phys_id, object_class, service_type_id, type_id, mapping, commit)
                if commit:
                    conn.commit()
            except Exception as ex:
                print(f'Exception occured: {ex}')
                if verbose:
                    traceback.print_exc()
                conn.rollback()
                results[i] = f'Skipped, caused exception: {ex}'
                skipped += 1
    objects['result'] = pd.Series(results)
    objects['functional_obj_id'] = pd.Series(functional_ids)
    print(f'Insertion finished. {len(objects)} objects processed: {added_as_points + added_to_building_adr + added_to_building_geom}'
        f' were added ({added_as_points} added as points, {added_to_building_adr} found buildings by address'
        f' and {added_to_building_geom} found buildings by geometry), {present} objects were already present, {skipped} objects were skipped')
    return objects

def replace_with_default(df: pd.DataFrame, default_values: Dict[str, Any]) -> pd.DataFrame:
    '''replace_with_default replace null items in dataframe in given columns with given values.

    `default_values` is a dictionary with columns names as key and default values for them as values.
    
    If column is missing, it will be created filled fully with default values

    Returns new dataframe with null entries replaced with given defaults
    '''
    for (column, value) in default_values.items():
        if column in df:
            df[column] = df[column].fillna(value)
        else:
            df[column] = pd.DataFrame([value] * df.shape[0])
    return df

def load_objects_geojson(filename: str, default_values: Optional[Dict[str, Any]] = None, needed_columns: Optional[Iterable[str]] = None) -> pd.DataFrame:
    '''load_objects_geojson loads objects as DataFrame from geojson. It contains only [features][properties] columns.
    Calls `replace_with_default` after load if `default_values` is present
    '''
    with open(filename, 'r', encoding='utf-8') as f:
        data = json.load(f)
        properties = pd.DataFrame(data['features'])['properties']
        res: pd.DataFrame = pd.DataFrame(map(lambda x: x.values(), properties), columns=properties[0].keys())
        if default_values is not None:
            res = replace_with_default(res, default_values)
        if needed_columns is not None:
            res = res[needed_columns]
        return res.where(pd.DataFrame.notnull(res), None)

def load_objects_json(filename: str, default_values: Optional[Dict[str, Any]] = None, needed_columns: Optional[Iterable[str]] = None) -> pd.DataFrame:
    '''load_objects_json loads objects as DataFrame from json by calling pd.read_json.
    Calls `replace_with_default` after load if `default_values` is present
    '''
    res: pd.DataFrame = pd.read_json(filename)
    if default_values is not None:
        res = replace_with_default(res, default_values)
    if needed_columns is not None:
        res = res[needed_columns]
    return res.where(pd.DataFrame.notnull(res), None)

def load_objects_csv(filename: str, default_values: Optional[Dict[str, Any]] = None, needed_columns: Optional[Iterable[str]] = None) -> pd.DataFrame:
    '''load_objects_csv loads objects as DataFrame from csv by calling pd.read_csv.
    Calls `replace_with_default` after load if `default_values` is present
    '''
    res: pd.DataFrame = pd.read_csv(filename)
    if default_values is not None:
        res = replace_with_default(res, default_values)
    if needed_columns is not None:
        res = res[needed_columns]
    return res.where(pd.DataFrame.notnull(res), None)

def load_objects_xlsx(filename: str, default_values: Optional[Dict[str, Any]] = None, needed_columns: Optional[Iterable[str]] = None) -> pd.DataFrame:
    '''load_objects_xlcx loads objects as DataFrame from xlsx by calling pd.read_excel (need to have `openpyxl` Pyhton module installed).
    Calls `replace_with_default` after load if `default_values` is present
    '''
    res: pd.DataFrame = pd.read_excel(filename, engine='openpyxl')
    if default_values is not None:
        res = replace_with_default(res, default_values)
    if needed_columns is not None:
        res = res[needed_columns]
    return res.where(pd.DataFrame.notnull(res), None)

def load_objects_excel(filename: str, default_values: Optional[Dict[str, Any]] = None, needed_columns: Optional[Iterable[str]] = None) -> pd.DataFrame:
    '''load_objects_excel loads objects as DataFrame from xls or ods by calling pd.read_excel (need to have `xlrd` Pyhton module installed).
    Calls `replace_with_default` after load if `default_values` is present
    '''
    res: pd.DataFrame = pd.read_excel(filename)
    if default_values is not None:
        res = replace_with_default(res, default_values)
    if needed_columns is not None:
        res = res[needed_columns]
    return res.where(pd.DataFrame.notnull(res), None)

def load_objects(filename: str, default_values: Optional[Dict[str, Any]] = None, needed_columns: Optional[Iterable[str]] = None) -> pd.DataFrame:
    funcs = {'csv': load_objects_csv, 'xlsx': load_objects_xlsx, 'xls': load_objects_excel,
            'ods': load_objects_excel, 'json': load_objects_json, 'geojson': load_objects_geojson}
    try:
        return funcs[filename[filename.rfind('.') + 1:]](filename)
    except KeyError:
        raise ValueError(f'File extension "{args.filename[args.filename.rfind(".") + 1:]}" is not supported')
    

if __name__ == '__main__':

    properties = Properties('localhost', 5432, 'citydb', 'postgres', 'postgres')

    parser = argparse.ArgumentParser(description='Inserts functional objects to the database')
    parser.add_argument('-H', '--db_addr', action='store', dest='db_addr',
                        help=f'postgres host address [default: {properties.db_addr}]', type=str)
    parser.add_argument('-P', '--db_port', action='store', dest='db_port',
                        help=f'postgres port number [default: {properties.db_port}]', type=int)
    parser.add_argument('-d', '--db_name', action='store', dest='db_name',
                        help=f'postgres database name [default: {properties.db_name}]', type=str)
    parser.add_argument('-U', '--db_user', action='store', dest='db_user',
                        help=f'postgres user name [default: {properties.db_user}]', type=str)
    parser.add_argument('-W', '--db_pass', action='store', dest='db_pass',
                        help=f'postgres user password [default: {properties.db_pass}]', type=str)

    parser.add_argument('-D', '--dry_run', action='store_true', dest='dry_run',
                        help=f'do not commit anything to the database, only output log file')
    parser.add_argument('-v', '--verbose', action='store_true', dest='verbose',
                        help=f'output stack trace when error happens')
    parser.add_argument('-l', '--log', action='store', dest='log_filename',
                        help=f'path to create log file [default: current datetime "YYYY-MM-DD HH-mm-ss-<filename>.csv"]', type=str)
    parser.add_argument('-c', '--class', action='store', dest='object_class',
                        help=f'object class (for <class>_objects + <class>_object_types tables) [required]', type=str, required=True)
    parser.add_argument('-T', '--type', action='store', dest='type',
                        help=f'service type name (for service_types table) [required]', type=str, required=True)
    parser.add_argument('-C', '--code', action='store', dest='code',
                        help=f'service type code (for service_types table) [required to insert, default null]', type=str)
    parser.add_argument('-f', '--city_function', action='store', dest='city_function',
                        help=f'name/code of city function of service type (from city_functions table) [required to insert, default null]', type=str)
    parser.add_argument('-m', '--min_capacity', action='store', dest='min_capacity',
                        help=f'service type minimum capacity (for service_types table) [default null]', type=int)
    parser.add_argument('-M', '--max_capacity', action='store', dest='max_capacity',
                        help=f'service type maximum capacity (for service_types table) [default null]', type=int)
    parser.add_argument('-t', '--types_file', action='store', dest='types',
                        help=f'path to json file with objects types ({{amenity: [type, code], ...}}) [default types.json]', type=str, default='types.json')
    parser.add_argument('-a', '--amenity', action='store', dest='default_amenity',
                        help=f'set objects amenity manually (if set, document\'s amenity field is fully ignored)', type=str)
    
    parser.add_argument('-dA', '--document_amenity', action='store', dest='amenity',
                        help=f' [default amenity]', type=str, default='amenity')
    parser.add_argument('-dx', '--document_latitude', action='store', dest='lat',
                        help=f' [default x]', type=str, default='x')
    parser.add_argument('-dy', '--document_longitude', action='store', dest='lng',
                        help=f' [default y]', type=str, default='y')
    parser.add_argument('-dN', '--document_name', action='store', dest='name',
                        help=f' [default name]', type=str, default='name')
    parser.add_argument('-dO', '--document_opening_hours', action='store', dest='opening_hours',
                        help=f' [default opening_hours]', type=str, default='opening_hours')
    parser.add_argument('-dW', '--document_website', action='store', dest='website',
                        help=f' [default contact:website]', type=str, default='contact:website')
    parser.add_argument('-dP', '--document_phone', action='store', dest='phone',
                        help=f' [default contact:phone]', type=str, default='contact:phone')
    parser.add_argument('-dAD', '--document_address', action='store', dest='address',
                        help=f' [default yand_adr]', type=str, default='yand_adr')
    parser.add_argument('-dAP', '--document_address_prefix', action='append', dest='adr_prefixes',
                        help=f' [default "Россия, Санкт-Петербург" (comma and space are not needed, adderess will be cut)], you can add multiple prefixed', type=str, default=[])
    parser.add_argument('-dI', '--document_osm_id', action='store', dest='osm_id',
                        help=f' [default id]', type=str, default='id')

    parser.add_argument('filename', action='store', help=f'path to file with data [required]', type=str)
    args = parser.parse_args()

    if len(args.adr_prefixes) == 0:
        args.adr_prefixes.append('Россия, Санкт-Петербург')
    else:
        args.adr_prefixes.sort(key=len, reverse=True)

    assert os.path.isfile(args.filename), 'Input file is not found. Exiting'

    if args.db_addr is not None:
        properties.db_addr = args.db_addr
    if args.db_port is not None:
        properties.db_port = args.db_port
    if args.db_name is not None:
        properties.db_name = args.db_name
    if args.db_user is not None:
        properties.db_user = args.db_user
    if args.db_pass is not None:
        properties.db_pass = args.db_pass
    if args.log_filename is None:
        t = time.localtime()
        fname = args.filename if os.path.sep not in os.path.relpath(args.filename) else args.filename[os.path.relpath(args.filename).rfind(os.path.sep) + 1:]
        logfile = f'{t.tm_year}-{t.tm_mon:02}-{t.tm_mday:02} ' \
                f'{t.tm_hour:02}-{t.tm_min:02}-{t.tm_sec:02}-' \
                f'{fname[:fname.rfind(".")] if "." in fname else fname}.csv'
        if os.path.isfile(logfile):
            logfile = f'{logfile[-4:]}-{time.time()}.csv'
        del fname
    else:
        logfile = args.log_filename

    mapping: Dict[str, str] = {'amenity': args.amenity, 'name': args.name, 'opening_hours': args.opening_hours,
            'website': args.website, 'phone': args.phone, 'address': args.address,
            'osm_id': args.osm_id, 'lat': args.lat, 'lng': args.lng}
    if args.default_amenity:
        del mapping['amenity']
        print(f'Objects amenity is set to {args.default_amenity}')
    print("Document's mapping:", mapping)

    with open(args.types, 'rt', encoding='utf-8') as f:
        types: Dict[str, Tuple[str, str]] = json.load(f)
    
    types = dict(map(lambda x: (x[0].lower(), x[1]), types.items()))

    print(f'Using database {properties.db_user}@{properties.db_addr}:{properties.db_port}/{properties.db_name}. ', end='')
    if args.dry_run:
        print('Dry run, no changes to database will be made')
    else:
        print('Objects will be written to the database')
    if args.verbose:
        print(f'Type pairs (<name in document\'s "{args.default_amenity or mapping["amenity"]}"> -> <name>, <code>):')
        for name_doc, (name, code) in types.items():
            print(f'\t{name_doc} -> {name}, {code}')
    print(f'Output log file - "{logfile}"')

    try:
        funcs = {'csv': load_objects_csv, 'xlsx': load_objects_xlsx, 'json': load_objects_json, 'geojson': load_objects_geojson}
        load_func = funcs[args.filename[args.filename.rfind('.') + 1:]]
    except KeyError:
        print(f'Cannot determine input file type by its extension (filename {args.filename}), use one of the (cvs, xlsx, json, geojson) formats')
        exit(1)
    del funcs
    objects: pd.DataFrame = load_func(args.filename, needed_columns=filter(lambda x: x not in ('', '-'), mapping.values()))
    print(f'Loaded {objects.shape[0]} objects from file "{args.filename}"')

    ensure_tables(properties.conn, args.object_class, not args.dry_run)
    service_id = ensure_service(properties.conn, args.type, args.code, args.min_capacity, args.max_capacity, args.city_function, not args.dry_run)
    objects = add_objects(properties.conn, objects, args.object_class, types, service_id, args.default_amenity, mapping, args.adr_prefixes, not args.dry_run, args.verbose)

    objects.to_csv(logfile)
    print(f'Finished, result is written to {logfile}')