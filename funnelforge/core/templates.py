"""Standard Desmond job files, for a job that arrives without them.

A structure imported on its own has no ``.msj`` and no ``.cfg``, and a
potential alone is not runnable.  These are the ordinary Desmond relaxation
chain and backend configuration, with the metadynamics production stage wired
to a FILE potential, so an export from a bare ``.cms`` is still a complete set.
Everything here is editable afterwards in the Job files tab.
"""

from __future__ import annotations

MSJ_TEMPLATE = """# {jobname}: bound-start WT Funnel-MetaD in explicit solvent.
# Times are in ps; energies are in kcal/mol.
# One direct Multisim chain: relaxation, equilibration, production.
# The supplied solvent box, waters, ions, force field and atom order are kept.

task {{
  task = "desmond:auto"
  set_family = {{
    desmond = {{
      checkpt.write_last_step = no
    }}
  }}
}}

simulate {{
  title       = "Brownian NVT, 10 K, restrained solute heavy atoms, 100 ps"
  annealing   = off
  time        = 100
  timestep    = [0.001 0.001 0.003]
  temperature = 10.0
  ensemble = {{
    class  = NVT
    method = Brownie
    brownie = {{
      delta_max = 0.1
    }}
  }}
  polarization_restraints = full
  restraints.new = [
    {{
      name            = posre_harm
      atoms           = solute_heavy_atom
      force_constants = 50.0
    }}
  ]
  eneseq.interval = 0.3
}}

simulate {{
  title                   = "NVT, 10 K, restrained solute heavy atoms, 12 ps"
  annealing               = off
  time                    = 12
  timestep                = [0.001 0.001 0.003]
  temperature             = 10.0
  polarization_restraints = full
  restraints.new = [
    {{
      name            = posre_harm
      atoms           = solute_heavy_atom
      force_constants = 50.0
    }}
  ]
  ensemble = {{
    class          = NVT
    method         = Langevin
    thermostat.tau = 0.1
  }}
  randomize_velocity.interval = 1.0
  eneseq.interval             = 0.3
  trajectory.center           = []
}}

simulate {{
  title                   = "NPT, 10 K, restrained solute heavy atoms, 12 ps"
  annealing               = off
  time                    = 12
  temperature             = 10.0
  polarization_restraints = full
  restraints.existing     = retain
  ensemble = {{
    class          = NPT
    method         = Langevin
    thermostat.tau = 0.1
    barostat.tau   = 50.0
  }}
  randomize_velocity.interval = 1.0
  eneseq.interval             = 0.3
  trajectory.center           = []
}}

simulate {{
  title                   = "NPT, {temperature:g} K, restrained solute heavy atoms, 12 ps"
  effect_if               = [["@*.*.annealing"] 'annealing = off temperature = "@*.*.temperature[0][0]"']
  time                    = 12
  polarization_restraints = full
  restraints.existing     = retain
  ensemble = {{
    class          = NPT
    method         = Langevin
    thermostat.tau = 0.1
    barostat.tau   = 50.0
  }}
  randomize_velocity.interval = 1.0
  eneseq.interval             = 0.3
  trajectory.center           = []
}}

simulate {{
  title     = "NPT, {temperature:g} K, unrestrained, 24 ps"
  effect_if = [["@*.*.annealing"] 'annealing = off temperature = "@*.*.temperature[0][0]"']
  time      = 24
  ensemble = {{
    class          = NPT
    method         = Langevin
    thermostat.tau = 0.1
    barostat.tau   = 2.0
  }}
  polarization_restraints = decay
  eneseq.interval          = 0.3
  trajectory.center        = solute
}}

# Production-chain equilibration, not a pilot or a separate trial job.
simulate {{
  title     = "Unbiased NPT equilibration at {temperature:g} K, {equil:g} ps"
  effect_if = [["@*.*.annealing"] 'annealing = off temperature = "@*.*.temperature[0][0]"']
  time      = {equil:g}
  ensemble = {{
    class          = NPT
    method         = Langevin
    thermostat.tau = 1.0
    barostat.tau   = 2.0
  }}
  polarization_restraints = none
  restraints.existing     = ignore
  randomize_velocity.interval = inf
  eneseq.interval             = 2.0
  trajectory.center           = solute
}}

simulate {{
  title     = "{production_ns:g} ns bound-start well-tempered Funnel-MetaD"
  cfg_file  = "{jobname}.cfg"
  jobname   = "$MAINJOBNAME"
  dir       = "."
  compress  = ""
  meta      = FILE
  meta_file = "{jobname}.pot"
  checkpt.write_last_step = yes
}}

# The custom FILE-based MetaD writes kerseq/cvseq itself.  No automatic native
# MetaD analysis stage is appended because it cannot reconstruct a custom CV.
"""

CFG_TEMPLATE = """annealing = false
backend = {{
}}
bigger_rclone = false
box = ?
bulk_properties = false
checkpt = {{
   first = 0.0
   interval = 500.0
   name = "$JOBNAME.cpt"
   write_last_step = true
}}
cpu = 1
cutoff_radius = 9.0
dipole_moment = false
ebias_force = false
elapsed_time = 0.0
energy_group = false
eneseq = {{
   first = 0.0
   interval = 2.0
   name = "$JOBNAME$[_replica$REPLICA$].ene"
}}
ensemble = {{
   barostat = {{
      tau = 2.0
   }}
   class = NPT
   method = MTK
   thermostat = {{
      tau = 1.0
   }}
}}
gaussian_force = false
glue = solute
lambda_dynamics = false
maeff_output = {{
   center_atoms = solute
   first = 0.0
   interval = 1000.0
   name = "$JOBNAME$[_replica$REPLICA$]-out.cms"
   periodicfix = true
   trjdir = "$JOBNAME$[_replica$REPLICA$]_trj"
}}
meta_file = ?
msd = false
polarization_restraints = none
pressure = [1.01325 isotropic]
pressure_tensor = false
randomize_velocity = {{
   first = 0.0
   interval = inf
   seed = {seed:d}
   temperature = "@*.temperature"
}}
restrain = none
restraints = {{
   existing = ignore
   new = []
}}
rnemd = false
simbox = {{
   first = 0.0
   interval = 2.0
   name = "$JOBNAME$[_replica$REPLICA$]_simbox.dat"
}}
spatial_temperature = false
surface_tension = 0.0
taper = false
temperature = [
   [{temperature:g} 0]
]
time = {time:g}
timestep = [0.002 0.002 0.006]
trajectory = {{
   center = solute
   first = 0.0
   format = dtr
   frames_per_file = 500
   interval = 20.0
   name = "$JOBNAME$[_replica$REPLICA$]_trj"
   periodicfix = true
   write_last_step = true
   write_last_vel = false
   write_velocity = false
}}
velocity_profile = false
wall_force = false
"""


def default_msj(jobname: str, temperature: float = 310.0,
                production_ps: float = 500000.0,
                equilibration_ps: float = 5000.0) -> str:
    return MSJ_TEMPLATE.format(jobname=jobname, temperature=temperature,
                               equil=equilibration_ps,
                               production_ns=production_ps / 1000.0)


def default_cfg(temperature: float = 310.0, production_ps: float = 500000.0,
                seed: int = 114197) -> str:
    return CFG_TEMPLATE.format(temperature=temperature, time=production_ps,
                               seed=int(seed))
