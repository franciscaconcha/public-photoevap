import os
import sys
import multiprocessing
import Queue
import threading
import numpy

from amuse.lab import *
from amuse.community.fractalcluster.interface import new_fractal_cluster_model
from amuse.ic.kingmodel import new_king_model

from disk_class import Disk
import FRIED_interp

G0 = 1.6e-3 * units.erg / units.s / units.cm**2

code_queue = Queue.Queue()


def distance(star1,
             star2):
    """ Return distance between star1 and star2

    :param star1: AMUSE particle
    :param star2: AMUSE particle
    :return: distance in units.parsec
    """
    return numpy.sqrt((star2.x - star1.x)**2 + (star2.y - star1.y)**2 + (star2.z - star1.z)**2)


def radiation_at_distance(rad, d):
    """ Return radiation rad at distance d

    :param rad: total radiation from star in erg/s
    :param d: distance in cm
    :return: radiation of star at distance d, in erg * s^-1 * cm^-2
    """
    return rad / (4 * numpy.pi * d**2) | (units.erg / (units.s * units.cm**2))


def photoevaporation_mass_loss(indices, nc):
    pool = multiprocessing.Pool(processes=nc)
    mass_losses = pool.map(single_photoevaporation_mass_loss, indices)
    pool.close()
    pool.join()
    return mass_losses


def single_photoevaporation_mass_loss(i):
    """ Return Mdot from FRIED grid.

    :param i: star key
    :return: Mdot (FUV + EUV) in MSun/yr
    """
    global stars, disks, disk_indices, interpolator
    this_star = stars[stars.key == i]
    this_disk = disks[disk_indices[i]]

    # FUV mass loss: interpolate from FRIED grid
    photoevap_Mdot_FUV = interpolator.interp_amuse(this_star.stellar_mass,
                                                   this_star.total_radiation | G0,
                                                   this_disk.disk_gas_mass,
                                                   this_disk.disk_radius)[0]

    # Check if there should be EUV mass loss as well; FRIED grid is FUV only
    # this_star.EUV is determined when the radiation on each star is calculated
    if this_star.EUV:
        # Photoevaporative mass loss in MSun/yr. Eq 20 from Johnstone, Hollenbach, & Bally 1998
        # From the paper: e ~ 3, x ~ 1.5
        photoevap_Mdot_EUV = 2. * 1E-9 * 3 * 4.12 * (this_star.disk_radius.value_in(units.cm) / 1E14) | units.MSun/units.yr
    else:
        photoevap_Mdot_EUV = 0.0 | units.MSun/units.yr

    this_star.EUV = False  # Back to false to recheck next time

    return photoevap_Mdot_FUV + photoevap_Mdot_EUV


def total_radiation(indices, nc):  # indices should be list of keys of small stars
    pool = multiprocessing.Pool(processes=nc)
    total_radiation = pool.map(single_total_radiation, indices)
    pool.close()
    pool.join()
    return total_radiation


def single_total_radiation(i):
    global stars
    this_star = stars[stars.key == i]  # Current star to calculate total radiation on

    # Calculate the total FUV contribution of the bright stars over each small star
    total_radiation = 0.0

    if i in stars[stars.bright].key:
        return total_radiation

    for s in stars[stars.bright]:  # For each massive/bright star
        # Calculate FUV luminosity of the bright star, in LSun
        lum = luminosity_fit(s.stellar_mass.value_in(units.MSun))

        # Calculate distance to bright star
        dist = distance(s, this_star)[0]

        # EUV regime -- Use Johnstone, Hollenbach, & Bally 1998
        dmin = 5. * 1E17 * 0.25 * numpy.sqrt(this_star.disk_radius.value_in(units.cm) / 1E14) | units.cm

        if dist < dmin:
            this_star.EUV = True
        # EUV mass loss will be calculated in mass_loss_parallel

        # FUV radiation only
        rad = lum.value_in(units.erg / units.s)
        d = dist.value_in(units.cm)
        radiation = rad / (4 * numpy.pi * d ** 2) | (units.erg / (units.s * units.cm ** 2))

        radiation_G0 = radiation.value_in(G0)
        total_radiation += radiation_G0

    return total_radiation


def luminosity_fit(mass):
    """
    Return stellar luminosity (in LSun) for corresponding mass, as calculated with Martijn's fit

    :param mass: stellar mass in MSun
    :return: stellar luminosity in LSun
    """
    if 0.12 < mass < 0.24:
        return (1.70294E16 * numpy.power(mass, 42.557)) | units.LSun
    elif 0.24 < mass < 0.56:
        return (9.11137E-9 * numpy.power(mass, 3.8845)) | units.LSun
    elif 0.56 < mass < 0.70:
        return (1.10021E-6 * numpy.power(mass, 12.237)) | units.LSun
    elif 0.70 < mass < 0.91:
        return (2.38690E-4 * numpy.power(mass, 27.199)) | units.LSun
    elif 0.91 < mass < 1.37:
        return (1.02477E-4 * numpy.power(mass, 18.465)) | units.LSun
    elif 1.37 < mass < 2.07:
        return (9.66362E-4 * numpy.power(mass, 11.410)) | units.LSun
    elif 2.07 < mass < 3.72:
        return (6.49335E-2 * numpy.power(mass, 5.6147)) | units.LSun
    elif 3.72 < mass < 10.0:
        return (6.99075E-1 * numpy.power(mass, 3.8058)) | units.LSun
    elif 10.0 < mass < 20.2:
        return (9.73664E0 * numpy.power(mass, 2.6620)) | units.LSun
    elif 20.2 < mass:
        return (1.31175E2 * numpy.power(mass, 1.7974)) | units.LSun
    else:
        return 0 | units.LSun


def periastron_distance(stars):
    """ Return the periastron distance of two encountering stars.

    :param stars: pair of encountering stars
    :return: periastron distance of the encounter
    """
    # Standard gravitational parameter
    mu = constants.G * (stars[0].mass + stars[1].mass)

    # Position vector from one star to the other
    r = stars[0].position - stars[1].position

    # Relative velocity between the stars
    v = stars[0].velocity - stars[1].velocity

    # Energy
    E = (v.length()) ** 2 / 2 - mu / r.length()

    # Semi-major axis
    a = -mu / 2 / E

    # Semi-latus rectum
    p = (numpy.cross(r.value_in(units.au),
                  v.value_in(units.m / units.s)) | units.au * units.m / units.s).length() ** 2 / mu

    # Eccentricity
    e = numpy.sqrt(1 - p / a)

    # Periastron distance
    return p / (1 + e)


def resolve_encounter(stars,
                      disks,
                      time,
                      mass_factor_exponent=0.2,
                      truncation_parameter=1. / 3,
                      gamma=1,
                      verbose=False):
    """ Resolve dynamical encounter between two stars.

    :param stars: pair of encountering stars, array of 2 AMUSE particles
    :param disk_codes: vader codes of the disks in the encounter
    :param time: time at which encounter occurs
    :param verbose: verbose option for debugging
    :return: updated vader disk codes
    """
    # For debugging
    if verbose:
        print(time.value_in(units.yr), stars.mass.value_in(units.MSun))

    closest_approach = periastron_distance(stars)

    # Check each star
    for i in range(2):
        # This is the collisional radius of the star as a particle in the dynamics code
        # We do this so that we don't detect this same encounter again in the next time step
        stars[i].collisional_radius = 0.49 * closest_approach

        if disks[i] is None:
            pass

        else:
            truncation_radius = closest_approach * truncation_parameter * \
                                ((stars[i].mass / stars[1 - i].mass) ** mass_factor_exponent)

            if truncation_radius < disks[i].disk_radius:
                print("Disk {0} should be truncated".format(i))
                print("Old radius: {0}, new radius: {1}".format(disks[i].disk_radius, truncation_radius))

                if truncation_radius <= 0.1 | units.au:
                    # Disk is dispersed. Have to handle this here or vader crashes for such small radii.
                    disks[i].dispersed = True
                    stars[i].disk_radius = truncation_radius
                    stars[i].disk_mass = 0.0 | units.MJupiter
                    stars[i].dispersal_time = time
                    stars[i].truncation_mass_loss = stars[i].disk_mass
                    stars[i].cumulative_truncation_mass_loss += stars[i].disk_mass

                #stars[i].stellar_mass += stars[i].initial_disk_mass - disk_mass(stars[i], time, gamma)
                new_disk = disks[i].truncate(truncation_radius)
                new_disk_radius = new_disk.disk_radius
                new_disk_mass = new_disk.disk_mass
                disks[i] = new_disk
                stars[i].last_encounter = time
                stars[i].disk_radius = new_disk_radius
                stars[i].disk_mass = new_disk_mass


def setup_disks_and_codes(star_keys,
                          disk_radii,
                          disk_masses,
                          stellar_masses,
                          dispersed_masses,
                          dispersed_densities,
                          number_of_vaders,
                          number_of_cells,
                          r_min,
                          r_max,
                          alpha,
                          mu=2.33):
    '''
    Setup up a number of disk objects and VADER integrators
    This is done in the same function as they need to exactly share grids

    disk_radii:       initial gas radius of each protoplanetary disk (vector shape (N), units of length)
    disk_masses:      initial gas masses of each protoplanetary disk (vector shape (N), units of mass)
    stellar_masses:   initial mass of host star of each protoplanetary disk (vector shape (N), units of mass)
    number_of_vaders: number of VADER integrators to initialize (integer)
    number_of_cells:  number of cells in grids of VADER integrators (integer)
    r_min:            inner edge of VADER grids (scalar, units of length)
    r_max:            inner edge of VADER grids (scalar, units of length)
    alpha:            dimensionless viscosity parameter (float)
    mu:               mean molecular mass of disk gas (float)
    Tm:               Disk midplane temperature at 1 AU (scalar, units of temperature)
    '''

    # This only happens in tests with small number of stars
    if number_of_vaders < len(stellar_masses):
        pass
    else:
        number_of_vaders = len(stellar_masses)

    viscous_codes = [vader(mode='none') for _ in range(number_of_vaders)]

    for i in range(number_of_vaders):
        viscous_codes[i].initialize_code()
        viscous_codes[i].initialize_keplerian_grid(number_of_cells, False, r_min, r_max, 1. | units.MSun)

        viscous_codes[i].parameters.alpha = alpha
        viscous_codes[i].parameters.post_timestep_function = True
        viscous_codes[i].parameters.maximum_tolerated_change = 1E99
        viscous_codes[i].parameters.number_of_user_parameters = 6
        viscous_codes[i].parameters.inner_pressure_boundary_torque = 0. | units.g * units.cm ** 2. / units.s ** 2.

        viscous_codes[i].set_parameter(2, 1E-12)
        Tm = 100. * (stellar_masses[i].value_in(units.MSun)) ** (1. / 4.) | units.K
        viscous_codes[i].set_parameter(3, Tm.value_in(units.K))
        viscous_codes[i].set_parameter(4, (mu * 1.008 * constants.u).value_in(units.g))

    number_of_disks = len(stellar_masses)

    disks = [Disk(star_keys[i],
                  disk_radii[i],
                  disk_masses[i],
                  stellar_masses[i],
                  dispersed_masses[i],
                  dispersed_densities[i],
                  viscous_codes[0].grid,
                  mu=mu,
                  external_photoevap_flag=True) for i in range(number_of_disks)]

    return viscous_codes, disks


def run_disks(viscous_codes, disks, dt):
    '''
    Evolve a set of disks for a time step, using a set of viscous codes

    viscous_codes: list of VADER codes
    disks: list of disk objects
    dt: time step to evolve for (scalar, units of time)
    '''

    # Distribute the disks evenly over the available codes
    disks_pooled = pool_disks(disks, len(viscous_codes))

    # Assign each group of disks to a process and start each process
    for i in range(len(viscous_codes)):

        code_queue.put({'disks': disks_pooled[i], 'viscous': viscous_codes[i], 'dt': dt})
        viscous_thread = threading.Thread(target=remote_worker_code)
        viscous_thread.daemon = True

        try:
            viscous_thread.start()
        except:
            print ("Thread could not be started; currently {a} threads are active".format(
                a=threading.active_count()))  # , flush=True)

    # Wait for all threads to finish
    code_queue.join()


def pool_disks(disks, N_cores):
    '''
    Distribute a set of disks among a number of processes

    disks: list of disk objects to distribute
    N_cores: number of cores to distribute disks over (int)

    returns a list of list of disks, such that the disks are divided as evenly as possible
    '''

    N = len(disks) // N_cores
    n = len(disks) % N_cores

    disks_pooled = []

    MIN = 0
    counter = 0

    for i in range(N_cores):

        if counter < n:
            DIFF = N + 1
            counter += 1
        else:
            DIFF = N

        disks_pooled.append([])
        disks_pooled[i].extend(disks[MIN:MIN + DIFF])

        MIN += DIFF

    return disks_pooled


def stop_codes(codes):
    '''
    Stop all codes in a list of codes
    '''

    for code in codes:
        code.stop()


def remote_worker_code():
    '''
    Worker function of each thread
    Receives a number of disks, a viscous code, and a time step, and evolves every disk for the time step using the code
    '''

    package = code_queue.get()

    disks = package['disks']
    code = package['viscous']
    dt = package['dt']

    for disk in disks:
        disk.viscous = code
        disk.evolve_disk_for(dt)

    code_queue.task_done()


def map_disk_indices_to_stars(disks):
    map = {}
    i = 0

    for d in disks:
        map[d.key] = i
        i += 1

    return map


global stars


def main(N,
         Rvir,
         Qvir,
         dist,
         alpha,
         ncells,
         t_ini,
         t_end,
         save_interval,
         ncores,
         save_path,
         grid_path,
         galaxy,
         distance_to_galactic_center,
         restart=0,
         ncodes=10,
         run_number=0):
    """ Run the simulation.

    :param N: number of stars
    :param Rvir: virial radius of cluster
    :param Qvir: virial ratio of cluster
    :param dist: spatial distribution: plummer, king, fractal
    :param alpha: turbulence parameter of disks
    :param ncells: number of cells for VADER disks
    :param t_ini: initial time of simulation
    :param t_end: final time of simulation
    :param save_interval: dt to save simulation snapshots
    :param ncores: number of cores to use
    :param save_path: path to save results
    :param grid_path: path to FRIED grid files
    :param galaxy: if True, use background galactic potential
    :param distance_to_galactic_center: distance to galactic center, for galactic potential calculation
    :param restart: if True, continue the simulation from the last saved snapshot
    :param ncodes: number of VADER codes to run in parallel
    :param run_number: run number, to save results separately for each run
    """

    try:
        float(t_end)
        t_end = t_end | units.Myr
    except TypeError:
        pass

    if restart:
        global stars, disks, disk_indices, interpolator

        path = "{0}/{1}/".format(save_path, run_number)
        files = [f for f in os.listdir(path) if (f.lower().endswith('.hdf5'))]
        files.sort(key=lambda f: float(f.split('t')[1].split('.hdf5')[0]))
        last_snapshot = files[-1]
        last_snapshot_t = float(last_snapshot.split('t')[1].split('.hdf5')[0])
        print("Continuing from from t = {0}".format(last_snapshot_t))

        f = '{0}/{1}'.format(path, last_snapshot)
        stars = read_set_from_file(f, 'hdf5', close_file=True)
        t = last_snapshot_t | t_end.unit
        print t
        converter = nbody_system.nbody_to_si(stars.stellar_mass.sum(), Rvir)

    else:
        t = 0.0 | t_end.unit

        path = "{0}/{1}/".format(save_path, run_number)
        try:
            os.makedirs(path)
            print("Results path created")
        except OSError as e:
            if e.errno != 17:
                raise
            pass

        max_stellar_mass = 100 | units.MSun
        stellar_masses = new_kroupa_mass_distribution(N, max_stellar_mass)#, random=False)
        converter = nbody_system.nbody_to_si(stellar_masses.sum(), Rvir)

        global stars, disks, disk_indices, interpolator

        # Spatial distribution, default is Plummer sphere
        if dist == "king":
            stars = new_king_model(N, W0=3, convert_nbody=converter)
        elif dist == "fractal":
            stars = new_fractal_cluster_model(N=N, fractal_dimension=1.6, convert_nbody=converter)
        else:
            stars = new_plummer_model(N, converter)

        stars.stellar_mass = stellar_masses
        stars.scale_to_standard(converter, virial_ratio=Qvir)

        # For small tests sometimes we don't get any stars > 1.9MSun, so we add one
        if len(stars[stars.stellar_mass >= 1.9 | units.MSun]) == 0:
            big_star = numpy.random.uniform(low=2, high=100)
            stars[0].stellar_mass = big_star | units.MSun
            print("Warning: No star with mass > 1.9 MSun generated by the IMF."
                  "\nOne star of {0} MSun added to the simulation.".format(big_star))

        # Bright stars: no disks; emit FUV radiation
        stars[stars.stellar_mass.value_in(units.MSun) > 1.9].bright = True
        stars[stars.stellar_mass.value_in(units.MSun) > 1.9].disked = False

        # Small stars: with disks; radiation from them not considered
        stars[stars.stellar_mass.value_in(units.MSun) <= 1.9].bright = False
        stars[stars.stellar_mass.value_in(units.MSun) <= 1.9].disked = True

        stars[stars.disked].disk_radius = 30 * (stars[stars.disked].stellar_mass.value_in(units.MSun) ** 0.5) | units.au
        stars[stars.disked].disk_mass = 0.1 * stars[stars.disked].stellar_mass

        stars[stars.bright].disk_radius = 0 | units.au
        stars[stars.bright].disk_mass = 0 | units.MSun

        stars.mass = stars.stellar_mass + stars.disk_mass  # Total particle mass is stellar mass + disk mass

        # Initially all stars have the same collisional radius
        stars.collisional_radius = 0.02 | units.parsec
        stars.encounters = 0  # Counter for dynamical encounters

        stars[stars.disked].cumulative_truncation_mass_loss = 0.0 | units.MSun
        stars[stars.disked].cumulative_photoevap_mass_loss = 0.0 | units.MSun

        # Saving initial G0 on small stars
        stars.g0 = 0.0

        # Flag for EUV photoevaporation mass loss
        stars.EUV = False

        stars[stars.disked].dispersed_mass_threshold = 0.03 | units.MEarth  # Ansdell+2016
        stars[stars.disked].dispersed_density_threshold = 1E-5 | units.g / units.cm**2  # Ingleby+ 2009

    # Create interpolator object for FRIED grid
    interpolator = FRIED_interp.FRIED_interpolator(folder=grid_path, verbosity=False)

    disk_codes, disks = setup_disks_and_codes(stars[stars.disked].key,
                                              stars[stars.disked].disk_radius,
                                              stars[stars.disked].disk_mass,
                                              stars[stars.disked].stellar_mass,
                                              stars[stars.disked].dispersed_mass_threshold,
                                              stars[stars.disked].dispersed_density_threshold,
                                              ncodes,  # number of vaders
                                              ncells,
                                              0.05 | units.au,
                                              2000 | units.au,
                                              5E-3)

    disk_indices = map_disk_indices_to_stars(disks)  # To keep track of disk in disks, which is a list

    # Doing this because the values in vader codes slightly differ from the theoretical initial disk values
    for key, val in disk_indices.items():
        stars[stars.key == key].disk_radius = disks[val].disk_radius
        stars[stars.key == key].disk_mass = disks[val].disk_mass

    # Start gravity code, add all stars
    if galaxy:  # If galactic potential is to be used
        print("Creating galactic potential + bridge")
        from amuse.couple import bridge
        from MilkyWayGalaxy import MilkyWayGalaxy

        stars.position += distance_to_galactic_center | units.parsec

        cluster = ph4(converter, number_of_workers=ncores)
        cluster.parameters.timestep_parameter = 0.01
        cluster.parameters.epsilon_squared = (100 | units.au) ** 2
        cluster.particles.add_particles(stars)

        # Enable stopping condition for dynamical encounters
        dynamical_encounter = cluster.stopping_conditions.collision_detection
        dynamical_encounter.enable()

        galaxy_potential = MilkyWayGalaxy()

        gravity = bridge.Bridge()#use_threading=False)
        gravity.add_system(cluster, (galaxy_potential,))
        gravity.timestep = 0.01 | units.Myr

        channel_from_gravity_to_framework = cluster.particles.new_channel_to(stars)
        channel_from_framework_to_gravity = stars.new_channel_to(cluster.particles,
                                                                 attributes=['collisional_radius'],
                                                                 target_names=['radius'])

    else:  # Cluster only, no galaxy
        gravity = ph4(converter, number_of_workers=ncores)
        gravity.parameters.timestep_parameter = 0.01
        gravity.parameters.epsilon_squared = (100 | units.au) ** 2
        gravity.particles.add_particles(stars)
        #gravity.model_time = t

        # Enable stopping condition for dynamical encounters
        dynamical_encounter = gravity.stopping_conditions.collision_detection
        dynamical_encounter.enable()

        channel_from_gravity_to_framework = gravity.particles.new_channel_to(stars)
        channel_from_framework_to_gravity = stars.new_channel_to(gravity.particles,
                                                                 attributes=['collisional_radius'],
                                                                 target_names=['radius'])

    # Start stellar evolution code, add only massive stars
    stellar = SeBa()
    stellar.parameters.metallicity = 0.02
    stellar.particles.add_particles(stars[stars.bright])

    # Enable stopping on supernova explosion
    detect_supernova = stellar.stopping_conditions.supernova_detection
    detect_supernova.enable()

    # Communication channels
    channel_from_stellar_to_framework = stellar.particles.new_channel_to(stars)
    channel_from_stellar_to_gravity = stellar.particles.new_channel_to(gravity.particles)

    channel_from_framework_to_stellar = stars.new_channel_to(stellar.particles)

    E_ini = gravity.kinetic_energy + gravity.potential_energy

    # For keeping track of energy
    E_handle = file('{0}/{1}/energy.txt'.format(save_path, run_number), 'a')
    Q_handle = file('{0}/{1}/virial.txt'.format(save_path, run_number), 'a')
    E_list = []
    Q_list = []

    write_set_to_file(stars,
                      '{0}/{1}/N{2}_t{3:.3f}.hdf5'.format(save_path,
                                                      run_number,
                                                      N,
                                                      t.value_in(units.Myr)),
                      'hdf5')

    channel_from_framework_to_gravity.copy()
    channel_from_stellar_to_framework.copy()
    channel_from_stellar_to_gravity.copy()
    channel_from_framework_to_stellar.copy()

    active_disks = len(stars[stars.disked])   # Counter for active disks
    dt = 1000 | units.yr

    # Evolve!
    while t < t_end:
        print("t = {0}".format(t))
        dt = min(dt, t_end - t)

        # First dt/2 for stellar evolution; copy to gravity and framework
        stellar.evolve_model(t + dt/2)
        channel_from_stellar_to_gravity.copy()
        channel_from_stellar_to_framework.copy()

        # TODO check for a better way to save the energies
        E_kin = gravity.kinetic_energy
        E_pot = gravity.potential_energy

        E_list.append([(E_kin + E_pot) / E_ini - 1])
        Q_list.append([-1.0 * E_kin / E_pot])

        gravity.evolve_model(t + dt)

        channel_from_gravity_to_framework.copy()

        while dynamical_encounter.is_set():  # Dynamical encounter detected
            encountering_stars = Particles(particles=[dynamical_encounter.particles(0)[0],
                                                      dynamical_encounter.particles(1)[0]])

            s0 = encountering_stars.get_intersecting_subset_in(stars)[0]
            s1 = encountering_stars.get_intersecting_subset_in(stars)[1]

            # This is to manage encounters involving bright stars (which have no associated vader code)
            if s0.disked and s1.disked:
                disk0 = disks[disk_indices[s0.key]]
                disk1 = disks[disk_indices[s1.key]]
                encountering_disks = [disk0, disk1]
                print("disked - disked")
                print("key0: {0}, mass0: {1}, disked0: {2}\n \
                      key1: {3}, mass1: {4}, disked1: {5}".format(s0.key,
                                                                  s0.stellar_mass.in_(units.MSun),
                                                                  s0.disked,
                                                                  s1.key,
                                                                  s1.stellar_mass.in_(units.MSun),
                                                                  s1.disked))
            elif s0.disked and not s1.disked:
                print("disked - bright or dispersed")
                print("key0: {0}, mass0: {1}, disked0: {2}\n \
                      key1: {3}, mass1: {4}, disked1: {5}".format(s0.key,
                                                                  s0.stellar_mass.in_(units.MSun),
                                                                  s0.disked,
                                                                  s1.key,
                                                                  s1.stellar_mass.in_(units.MSun),
                                                                  s1.disked))

                disk0 = disks[disk_indices[s0.key]]
                encountering_disks = [disk0, None]

            elif not s0.disked and s1.disked:
                print("bright or dispersed - disked")
                print("key0: {0}, mass0: {1}, disked0: {2}\n \
                      key1: {3}, mass1: {4}, disked1: {5}".format(s0.key,
                                                                  s0.stellar_mass.in_(units.MSun),
                                                                  s0.disked,
                                                                  s1.key,
                                                                  s1.stellar_mass.in_(units.MSun),
                                                                  s1.disked))

                disk1 = disks[disk_indices[s1.key]]
                encountering_disks = [None, disk1]
            else:
                print("bright - bright or dispersed - dispersed")

                print("key0: {0}, mass0: {1}, disked0: {2}\n \
                      key1: {3}, mass1: {4}, disked1: {5}".format(s0.key,
                                                                  s0.stellar_mass.in_(units.MSun),
                                                                  s0.disked,
                                                                  s1.key,
                                                                  s1.stellar_mass.in_(units.MSun),
                                                                  s1.disked))

                encountering_disks = [None, None]

            resolve_encounter([s0, s1],
                              encountering_disks,
                              gravity.model_time + t_ini)

            gravity.evolve_model(t + dt)

        # Copy stars' new collisional radii (updated in resolve_encounter) to gravity
        channel_from_framework_to_gravity.copy()

        ########### Photoevaporation ############

        # Calculate the total FUV contribution of the bright stars over each small star
        stars[stars.disked].total_radiation = total_radiation(stars[stars.disked].key, ncores)

        # Photoevaporative mass loss in log10(MSun/yr), EUV + FUV
        stars[stars.disked].photoevap_Mdot = photoevaporation_mass_loss(stars[stars.disked].key, ncores)
        stars[stars.disked].cumulative_photoevap_mass_loss += stars[stars.disked].photoevap_Mdot * dt

        # Update disks' mass loss rates before evolving them
        for k in disk_indices:
            disks[disk_indices[k]].outer_photoevap_rate = stars[stars.key == k].photoevap_Mdot

        # Evolve VADER disks
        # This evolution includes gas+dust evolution and external photoevaporation
        run_disks(disk_codes, [d for d in disks if not d.dispersed], dt)

        # Update stars' disks parameters, for book-keeping
        for this_star in stars[stars.disked]:
            this_disk = disks[disk_indices[this_star.key]]
            this_star.disked = not this_disk.dispersed
            this_star.disk_radius = this_disk.disk_radius
            this_star.disk_dust_mass = this_disk.disk_dust_mass
            this_star.disk_gas_mass = this_disk.disk_gas_mass
            this_star.disk_mass = this_disk.disk_mass

        ########### End Photoevaporation  ############

        channel_from_framework_to_gravity.copy()

        # Second dt/2 for stellar evolution; copy to gravity and framework
        stellar.evolve_model(t + dt/2)
        channel_from_stellar_to_gravity.copy()
        channel_from_stellar_to_framework.copy()

        print "Before t+=dt: t = {0}, model time = {1:.3f}, {1}".format(t,
                                                                   gravity.model_time.value_in(units.Myr))
        t += dt

        active_disks = len([d for d in disks if not d.dispersed])

        if active_disks <= 0:
            write_set_to_file(stars,
                              '{0}/{1}/N{2}_t{3}.hdf5'.format(save_path,
                                                              run_number,
                                                              N,
                                                              t.value_in(units.Myr)),
                              'hdf5')
            print("NO DISKS LEFT AT t = {0} Myr".format(t.value_in(units.Myr)))
            print("saving! at t = {0} Myr".format(t.value_in(units.Myr)))
            break

        if (numpy.around(t.value_in(units.yr)) % save_interval.value_in(units.yr)) == 0.:
            print("saving! at t = {0} Myr".format(t.value_in(units.Myr)))
            write_set_to_file(stars,
                              '{0}/{1}/N{2}_t{3:.3f}.hdf5'.format(save_path,
                                                          run_number,
                                                          N,
                                                          t.value_in(units.Myr)),
                              'hdf5')

        numpy.savetxt(E_handle, E_list)
        numpy.savetxt(Q_handle, Q_list)

        E_list = []
        Q_list = []

    if active_disks > 0:
        print("SIMULATION ENDED AT t = {0} Myr".format(t_end.value_in(units.Myr)))

    for d in disk_codes:
        d.stop()

    gravity.stop()
    stellar.stop()


def new_option_parser():
    from amuse.units.optparse import OptionParser
    result = OptionParser()

    # Simulation parameters
    result.add_option("-n", dest="ncores", type="int", default=1,
                      help="number of cores [%default]")
    result.add_option("-s", dest="save_path", type="string", default='.',
                      help="path to save the results [%default]")
    result.add_option("-i", dest="save_interval", type="int", default=5000 | units.yr,
                      help="time interval of saving a snapshot of the cluster [%default]")
    result.add_option("-f", dest="grid_path", type="string", default='data',
                      help="path for FRIED grid [%default]")
    result.add_option("-r", dest="run_number", type="int", default=0,
                      help="run number [%default]")
    result.add_option("--re", dest="restart", type="int", default=0,
                      help="restart from last snapshot? [%default]")

    # Cluster parameters
    result.add_option("-N", dest="N", type="int", default=100,
                      help="number of stars [%default]")
    result.add_option("-R", dest="Rvir", type="float",
                      unit=units.parsec, default=0.5,
                      help="cluster virial radius [%default]")
    result.add_option("-Q", dest="Qvir", type="float", default=0.5,
                      help="virial ratio [%default]")
    result.add_option("-p", dest="dist", type="string", default="plummer",
                      help="spatial distribution [%default]")
    result.add_option("-g", dest="galaxy", type="int", default="0",
                      help="True to add Milky Way galactic potential [%default]")
    result.add_option("-d", dest="distance_to_galactic_center", type="float", default=0.0,
                      help="When using galactic potential, ('projected') distance to galactic center [%default]")

    # Disk parameters
    result.add_option("-a", dest="alpha", type="float", default=5E-3,
                      help="turbulence parameter [%default]")
    result.add_option("-c", dest="ncells", type="int", default=100,
                      help="Number of cells to be used in vader disk [%default]")

    # Time parameters
    result.add_option("-I", dest="t_ini", type="int", default=0 | units.yr,
                      help="initial time [%default]")
    #result.add_option("-t", dest="dt", type="int", default=1000 | units.yr,
    #                  help="dt for simulation [%default]")
    result.add_option("-e", dest="t_end", type="float", default=2 | units.Myr,
                      help="end time of the simulation [%default]")

    return result


if __name__ == '__main__':
    o, arguments = new_option_parser().parse_args()
    main(**o.__dict__)
