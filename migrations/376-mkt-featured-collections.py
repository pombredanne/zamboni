import amo
from bandwagon.models import Collection, FeaturedCollection
from users.models import UserProfile

def run():
    a, created = UserProfile.objects.get_or_create(username="mozilla")
    Collection.objects.get_or_create(author=a,
                                     slug="webapps_home", type=amo.COLLECTION_FEATURED,
                                     listed=False)
    Collection.objects.get_or_create(author=a,
                                     slug="webapps_featured", type=amo.COLLECTION_FEATURED,
                                     listed=False)
